// input_helper -- the hands for Mira's computer control (daemon/computer_agent.py).
//
// One short-lived invocation per action, spawned by Mira.app, so it runs under
// Mira's own Accessibility grant (macOS attributes a child process to the app
// that launched it). Posting synthetic events without that grant silently does
// nothing, so every command that posts events checks for it first and says so.
//
// Coordinates are global screen points with a top-left origin -- the same
// space Electron's screen API and CGEvent both use.
//
//   click <x> <y> [left|right] [count]   move, then click (count 2 = double)
//   move <x> <y>
//   type <text>                          unicode-safe, independent of keyboard layout
//   key <combo>                          e.g. cmd+s, return, shift+tab, cmd+shift+4
//   scroll <dy> [dx]                     lines; negative dy scrolls down
//   cursor                               {"x","y"} of the mouse right now
//   frontmost                            {"app","bundle"} of the frontmost app
//   trusted                              whether events can be posted at all
//
// Every command prints one line of JSON: {"ok": true, ...} or {"ok": false, "error": ...}.

import AppKit
import ApplicationServices
import Foundation

func emit(_ obj: [String: Any]) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: obj)) ?? Data("{\"ok\":false}".utf8)
    print(String(data: data, encoding: .utf8) ?? "{\"ok\":false}")
    exit(obj["ok"] as? Bool == true ? 0 : 1)
}

func fail(_ message: String) -> Never { emit(["ok": false, "error": message]) }

func requireTrust() {
    if !AXIsProcessTrusted() {
        fail("Mira doesn't have Accessibility permission, so it can't click or type. " +
             "Grant it in Settings > Permissions, then restart Mira.")
    }
}

let source = CGEventSource(stateID: .hidSystemState)

func post(_ event: CGEvent?) {
    event?.post(tap: .cghidEventTap)
}

func pause(_ ms: UInt32) { usleep(ms * 1000) }

func point(_ xs: String, _ ys: String) -> CGPoint {
    guard let x = Double(xs), let y = Double(ys) else { fail("x and y must be numbers") }
    return CGPoint(x: x, y: y)
}

let keyCodes: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "forwarddelete": 117, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
]

let modifierFlags: [String: CGEventFlags] = [
    "cmd": .maskCommand, "command": .maskCommand, "shift": .maskShift,
    "alt": .maskAlternate, "option": .maskAlternate, "opt": .maskAlternate,
    "ctrl": .maskControl, "control": .maskControl, "fn": .maskSecondaryFn,
]

var args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { fail("usage: input_helper <command> ...") }
args.removeFirst()

switch command {
case "trusted":
    emit(["ok": true, "trusted": AXIsProcessTrusted()])

case "cursor":
    let loc = CGEvent(source: nil)?.location ?? .zero
    emit(["ok": true, "x": loc.x, "y": loc.y])

case "frontmost":
    let app = NSWorkspace.shared.frontmostApplication
    emit(["ok": true, "app": app?.localizedName ?? "", "bundle": app?.bundleIdentifier ?? ""])

case "move":
    requireTrust()
    guard args.count >= 2 else { fail("usage: move <x> <y>") }
    post(CGEvent(mouseEventSource: source, mouseType: .mouseMoved,
                 mouseCursorPosition: point(args[0], args[1]), mouseButton: .left))
    emit(["ok": true])

case "click":
    requireTrust()
    guard args.count >= 2 else { fail("usage: click <x> <y> [left|right] [count]") }
    let p = point(args[0], args[1])
    let right = args.count > 2 && args[2] == "right"
    let count = max(1, min(3, args.count > 3 ? Int(args[3]) ?? 1 : 1))
    let (down, up, button): (CGEventType, CGEventType, CGMouseButton) =
        right ? (.rightMouseDown, .rightMouseUp, .right) : (.leftMouseDown, .leftMouseUp, .left)
    // Move first so the target gets its hover state, as a real pointer would.
    post(CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: button))
    pause(40)
    for n in 1...count {
        let d = CGEvent(mouseEventSource: source, mouseType: down, mouseCursorPosition: p, mouseButton: button)
        let u = CGEvent(mouseEventSource: source, mouseType: up, mouseCursorPosition: p, mouseButton: button)
        d?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
        u?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
        post(d); pause(25); post(u)
        if n < count { pause(60) }
    }
    emit(["ok": true])

case "type":
    requireTrust()
    let text = args.joined(separator: " ")
    // Chunks of 16 UTF-16 units: the most a single keyboard event carries.
    let units = Array(text.utf16)
    var i = 0
    while i < units.count {
        var chunk = Array(units[i..<min(i + 16, units.count)])
        for keyDown in [true, false] {
            let ev = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: keyDown)
            ev?.keyboardSetUnicodeString(stringLength: chunk.count, unicodeString: &chunk)
            post(ev)
        }
        pause(12)
        i += 16
    }
    emit(["ok": true, "typed": units.count])

case "key":
    requireTrust()
    guard let combo = args.first?.lowercased() else { fail("usage: key <combo>") }
    var flags: CGEventFlags = []
    var code: CGKeyCode? = nil
    for part in combo.split(separator: "+").map(String.init) {
        if let f = modifierFlags[part] { flags.insert(f) }
        else if let c = keyCodes[part] { code = c }
        else { fail("unknown key '\(part)'") }
    }
    guard let keyCode = code else { fail("no key in '\(combo)'") }
    for keyDown in [true, false] {
        let ev = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: keyDown)
        ev?.flags = flags
        post(ev)
        pause(15)
    }
    emit(["ok": true])

case "scroll":
    requireTrust()
    let dy = Int32(args.first.flatMap { Int($0) } ?? -3)
    let dx = Int32(args.count > 1 ? Int(args[1]) ?? 0 : 0)
    post(CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2,
                 wheel1: dy, wheel2: dx, wheel3: 0))
    emit(["ok": true])

default:
    fail("unknown command '\(command)'")
}
