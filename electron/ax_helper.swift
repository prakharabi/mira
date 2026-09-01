import Cocoa
import ApplicationServices

func getFocusedElement() -> AXUIElement? {
    guard let frontApp = NSWorkspace.shared.frontmostApplication else { return nil }
    let appElement = AXUIElementCreateApplication(frontApp.processIdentifier)

    var focusedElement: AnyObject?
    let result = AXUIElementCopyAttributeValue(appElement, kAXFocusedUIElementAttribute as CFString, &focusedElement)

    guard result == .success, let element = focusedElement else { return nil }
    return (element as! AXUIElement)
}

// Some apps report a "successful" caret bounds lookup with a placeholder/incorrect
// rect instead of a real error (seen e.g. in some Electron/Chromium apps). Getting
// the frontmost WINDOW's own bounds lets the caller sanity-check the reported
// caret position actually falls inside it, catching those false-success cases
// that a bare AXError check alone can't.
func getFrontWindowBounds() -> CGRect? {
    guard let frontApp = NSWorkspace.shared.frontmostApplication else { return nil }
    let appElement = AXUIElementCreateApplication(frontApp.processIdentifier)

    var windowValue: AnyObject?
    let result = AXUIElementCopyAttributeValue(appElement, kAXFocusedWindowAttribute as CFString, &windowValue)
    guard result == .success, let window = windowValue else { return nil }
    let windowElement = window as! AXUIElement

    var positionValue: AnyObject?
    var sizeValue: AnyObject?
    guard AXUIElementCopyAttributeValue(windowElement, kAXPositionAttribute as CFString, &positionValue) == .success,
          AXUIElementCopyAttributeValue(windowElement, kAXSizeAttribute as CFString, &sizeValue) == .success else {
        return nil
    }

    var origin = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue((positionValue as! AXValue), .cgPoint, &origin),
          AXValueGetValue((sizeValue as! AXValue), .cgSize, &size) else {
        return nil
    }

    return CGRect(origin: origin, size: size)
}

func getStringAttribute(_ element: AXUIElement, _ attribute: String) -> String? {
    var value: AnyObject?
    let result = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    guard result == .success else { return nil }
    return value as? String
}

func getNumberAttribute(_ element: AXUIElement, _ attribute: String) -> Int? {
    var value: AnyObject?
    let result = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    guard result == .success else { return nil }
    if let num = value as? NSNumber { return num.intValue }
    return nil
}

func frontmostBundleIdJSON() -> String {
    guard let id = NSWorkspace.shared.frontmostApplication?.bundleIdentifier else { return "null" }
    return "\"\(id)\""
}

func readContext() {
    // Reading the frontmost app's bundle ID here (a cheap, in-process NSWorkspace
    // call) means the caller no longer needs a separate `osascript` subprocess just
    // to check which app is focused -- that used to run on every single poll tick,
    // indefinitely, even while idle. Computed first so it's included even when
    // there's no focused text element to read (e.g. just browsing, no field focused).
    let bundleId = frontmostBundleIdJSON()

    guard let el = getFocusedElement() else {
        print("{\"error\":\"no focused element\",\"bundleId\":\(bundleId)}")
        return
    }

    let text = getStringAttribute(el, kAXValueAttribute as String) ?? ""

    var selectedRange: AnyObject?
    var rangeStart = text.count
    let rangeResult = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute as CFString, &selectedRange)
    if rangeResult == .success, let rangeValue = selectedRange {
        var cfRange = CFRange()
        if AXValueGetValue((rangeValue as! AXValue), .cfRange, &cfRange) {
            rangeStart = cfRange.location
        }
    }

    let textBeforeCursor = String(text.prefix(rangeStart))

    let escaped = textBeforeCursor
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
        .replacingOccurrences(of: "\n", with: "\\n")

    // get actual caret screen position via kAXBoundsForRangeParameterizedAttribute
    var caretX = -1.0
    var caretY = -1.0
    var boundsErrorCode = -999
    if rangeResult == .success, let rangeValue = selectedRange {
        var boundsValue: AnyObject?
        let boundsResult = AXUIElementCopyParameterizedAttributeValue(
            el,
            kAXBoundsForRangeParameterizedAttribute as CFString,
            rangeValue,
            &boundsValue
        )
        boundsErrorCode = Int(boundsResult.rawValue)
        if boundsResult == .success, let bounds = boundsValue {
            var rect = CGRect.zero
            if AXValueGetValue((bounds as! AXValue), .cgRect, &rect) {
                caretX = rect.origin.x
                caretY = rect.origin.y
            }
        }
    }

    var windowJSON = "null"
    if let win = getFrontWindowBounds() {
        windowJSON = "{\"x\":\(win.origin.x),\"y\":\(win.origin.y),\"width\":\(win.size.width),\"height\":\(win.size.height)}"
    }

    print("{\"textBeforeCursor\":\"\(escaped)\",\"caretX\":\(caretX),\"caretY\":\(caretY),\"boundsError\":\(boundsErrorCode),\"window\":\(windowJSON),\"bundleId\":\(bundleId)}")
}

// Types text by posting synthetic keyboard events, exactly like a real keystroke,
// rather than via the Accessibility "set value" API. Most apps (browsers,
// Electron apps, and most modern non-Cocoa-native apps) only implement the
// Accessibility READ side, not the WRITE side, which is why AX-based insertion
// (AXUIElementSetAttributeValue on kAXSelectedTextAttribute) only ever worked in
// a handful of apps like TextEdit. Synthetic keyboard events go through the same
// input pipeline as real typing, so they're received correctly almost anywhere.
@discardableResult
func postUnicodeText(_ text: String) -> Bool {
    let source = CGEventSource(stateID: .combinedSessionState)
    let utf16Chars = Array(text.utf16)
    guard !utf16Chars.isEmpty else { return true }

    guard let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
          let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
        return false
    }

    keyDown.keyboardSetUnicodeString(stringLength: utf16Chars.count, unicodeString: utf16Chars)
    keyUp.keyboardSetUnicodeString(stringLength: utf16Chars.count, unicodeString: utf16Chars)

    keyDown.post(tap: .cgSessionEventTap)
    keyUp.post(tap: .cgSessionEventTap)
    return true
}

func typeText(_ text: String) {
    if postUnicodeText(text) {
        print("{\"success\":true}")
    } else {
        print("{\"error\":\"could not create synthetic keyboard event\"}")
    }
}

// Backspace as a real key event (not a unicode string) -- deletes one character
// at a time, same as the user pressing Delete themselves.
func postBackspace() {
    let source = CGEventSource(stateID: .combinedSessionState)
    let kVKDelete: CGKeyCode = 51
    guard let keyDown = CGEvent(keyboardEventSource: source, virtualKey: kVKDelete, keyDown: true),
          let keyUp = CGEvent(keyboardEventSource: source, virtualKey: kVKDelete, keyDown: false) else { return }
    keyDown.post(tap: .cgSessionEventTap)
    keyUp.post(tap: .cgSessionEventTap)
}

// Deletes `deleteCount` characters then types `replacement` -- used to apply a
// spelling correction to a word already on the page (delete the wrong one,
// type the right one), via the same synthetic-keyboard-event path as insert.
//
// Small delays between events are deliberate, not incidental: posting a burst
// of CGEvents back-to-back with zero delay was observed to drop/coalesce
// events in practice (a real backspace+retype produced "I reciev" instead of
// "I receive " in testing -- some events in the burst never landed). A few ms
// between each backspace, and a slightly longer pause before the final type,
// gives the target app's event loop time to actually process each one.
func correctWord(deleteCount: Int, replacement: String) {
    guard deleteCount >= 0, deleteCount <= 200 else {
        print("{\"error\":\"invalid deleteCount\"}")
        return
    }
    for _ in 0..<deleteCount {
        postBackspace()
        usleep(8000) // 8ms
    }
    usleep(15000) // 15ms settle before retyping
    if postUnicodeText(replacement) {
        print("{\"success\":true}")
    } else {
        print("{\"error\":\"could not create synthetic keyboard event\"}")
    }
}

func jsonEscape(_ s: String) -> String {
    s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
}

// Uses macOS's own spell checker (the same one behind right-click "Spelling"
// everywhere else on the system) -- native, fast, no network call, and already
// configured for whatever languages the user has enabled.
func spellcheck(_ word: String) {
    let checker = NSSpellChecker.shared
    // the default ambient checkSpelling(of:startingAt:) proved too lenient in
    // testing (missed "teh", "adress", "wich") -- forcing an explicit language
    // and using the full overload is meaningfully stricter and catches these
    checker.automaticallyIdentifiesLanguages = false
    let language = "en_US"
    let range = checker.checkSpelling(of: word, startingAt: 0, language: language, wrap: false, inSpellDocumentWithTag: 0, wordCount: nil)

    if range.location == NSNotFound {
        print("{\"misspelled\":false}")
        return
    }

    let guesses = checker.guesses(forWordRange: range, in: word, language: language, inSpellDocumentWithTag: 0) ?? []
    if let suggestion = guesses.first {
        print("{\"misspelled\":true,\"suggestion\":\"\(jsonEscape(suggestion))\"}")
    } else {
        print("{\"misspelled\":true,\"suggestion\":null}")
    }
}

let args = CommandLine.arguments

if args.count >= 2 && args[1] == "read" {
    readContext()
} else if args.count >= 3 && args[1] == "insert" {
    typeText(args[2])
} else if args.count >= 4 && args[1] == "correct", let deleteCount = Int(args[2]) {
    correctWord(deleteCount: deleteCount, replacement: args[3])
} else if args.count >= 3 && args[1] == "spellcheck" {
    spellcheck(args[2])
} else {
    print("{\"error\":\"usage: ax_helper read | insert <text> | correct <deleteCount> <replacement> | spellcheck <word>\"}")
}
