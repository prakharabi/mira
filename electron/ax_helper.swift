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

    // Whether anything follows the caret on the SAME line. Inline suggestions
    // are drawn over the app's own window, so with text after the caret they'd
    // sit on top of it and both become unreadable -- the caller falls back to
    // showing the suggestion below the line in that case.
    let remainder = String(text.dropFirst(min(rangeStart, text.count)))
    let restOfLine = remainder.prefix(while: { $0 != "\n" })
    let hasTextAfterCaret = !restOfLine.trimmingCharacters(in: .whitespaces).isEmpty

    let escaped = jsonEscape(textBeforeCursor)

    // Caret screen position.
    //
    // A zero-length range is the obvious thing to ask for, and it's the wrong
    // thing to rely on: in TextEdit it comes back one line height too high
    // (a coordinate-convention mismatch), and in Chromium-based apps every
    // variant returns an all-zero placeholder. Measuring a SINGLE CHARACTER
    // instead gives a rect whose y and height are correct, and whose horizontal
    // edge is exactly where the caret sits.
    //
    // The height matters as much as the origin: it's the line height at the
    // caret, and the only signal available for how large the target app's text
    // is, which is what lets a suggestion be drawn at a matching size.
    var caretX = -1.0
    var caretY = -1.0
    var caretH = 0.0
    var boundsErrorCode = -999

    func boundsForRange(_ location: Int, _ length: Int) -> CGRect? {
        var range = CFRange(location: location, length: length)
        guard let axRange = AXValueCreate(.cfRange, &range) else { return nil }
        var out: AnyObject?
        let res = AXUIElementCopyParameterizedAttributeValue(
            el, kAXBoundsForRangeParameterizedAttribute as CFString, axRange, &out)
        guard res == .success, let o = out else { return nil }
        var rect = CGRect.zero
        guard AXValueGetValue((o as! AXValue), .cgRect, &rect) else { return nil }
        // An all-zero or zero-height rect is a placeholder, not a measurement.
        guard rect.size.height > 0 else { return nil }
        return rect
    }

    if rangeStart >= 0 {
        let totalChars = text.count

        if rangeStart < totalChars, let r = boundsForRange(rangeStart, 1) {
            // Character the caret sits in front of: its left edge IS the caret,
            // and it's guaranteed to be on the caret's own line.
            caretX = r.origin.x
            caretY = r.origin.y
            caretH = r.size.height
            boundsErrorCode = 0
        } else if rangeStart > 0, let r = boundsForRange(rangeStart - 1, 1) {
            // Caret at end of text: measure the character behind it and take
            // its right edge.
            caretX = r.origin.x + r.size.width
            caretY = r.origin.y
            caretH = r.size.height
            boundsErrorCode = 0
        } else if rangeResult == .success, let rangeValue = selectedRange {
            // Last resort. This is the path with the flipped-origin quirk, so
            // the height is added back to land on the real line.
            var boundsValue: AnyObject?
            let res = AXUIElementCopyParameterizedAttributeValue(
                el, kAXBoundsForRangeParameterizedAttribute as CFString, rangeValue, &boundsValue)
            boundsErrorCode = Int(res.rawValue)
            if res == .success, let bounds = boundsValue {
                var rect = CGRect.zero
                if AXValueGetValue((bounds as! AXValue), .cgRect, &rect), rect.size.height > 0 {
                    caretX = rect.origin.x
                    caretY = rect.origin.y + rect.size.height
                    caretH = rect.size.height
                }
            }
        }
    }

    // The focused text element's own frame. When the caret rect is missing or a
    // placeholder, anchoring to the text field itself is still far better than
    // falling back to the mouse pointer, which can be anywhere on screen.
    var elementJSON = "null"
    var elPos: AnyObject?
    var elSize: AnyObject?
    if AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &elPos) == .success,
       AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &elSize) == .success {
        var origin = CGPoint.zero
        var size = CGSize.zero
        if AXValueGetValue((elPos as! AXValue), .cgPoint, &origin),
           AXValueGetValue((elSize as! AXValue), .cgSize, &size) {
            elementJSON = "{\"x\":\(origin.x),\"y\":\(origin.y),\"width\":\(size.width),\"height\":\(size.height)}"
        }
    }

    var windowJSON = "null"
    if let win = getFrontWindowBounds() {
        windowJSON = "{\"x\":\(win.origin.x),\"y\":\(win.origin.y),\"width\":\(win.size.width),\"height\":\(win.size.height)}"
    }

    print("{\"textBeforeCursor\":\"\(escaped)\",\"caretX\":\(caretX),\"caretY\":\(caretY),\"caretH\":\(caretH),\"hasTextAfterCaret\":\(hasTextAfterCaret),\"boundsError\":\(boundsErrorCode),\"window\":\(windowJSON),\"element\":\(elementJSON),\"bundleId\":\(bundleId)}")
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

// JSON forbids raw control characters inside strings. Escaping only quotes,
// backslashes and newlines is not enough: a single TAB in the user's text
// produced output that JSON.parse rejected, which silently killed the whole
// predictive-typing loop until that tab was deleted. Anything below U+0020 has
// to be escaped, so this handles the named cases and falls back to \uXXXX.
func jsonEscape(_ s: String) -> String {
    var out = ""
    out.reserveCapacity(s.count + 16)
    for scalar in s.unicodeScalars {
        switch scalar {
        case "\\": out += "\\\\"
        case "\"": out += "\\\""
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        case "\u{08}": out += "\\b"
        case "\u{0C}": out += "\\f"
        default:
            if scalar.value < 0x20 {
                out += String(format: "\\u%04x", scalar.value)
            } else {
                out.unicodeScalars.append(scalar)
            }
        }
    }
    return out
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
