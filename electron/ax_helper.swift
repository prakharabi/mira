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

func readContext() {
    guard let el = getFocusedElement() else {
        print("{\"error\":\"no focused element\"}")
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

    print("{\"textBeforeCursor\":\"\(escaped)\",\"caretX\":\(caretX),\"caretY\":\(caretY),\"boundsError\":\(boundsErrorCode),\"window\":\(windowJSON)}")
}

// Types text by posting synthetic keyboard events, exactly like a real keystroke,
// rather than via the Accessibility "set value" API. Most apps (browsers,
// Electron apps, and most modern non-Cocoa-native apps) only implement the
// Accessibility READ side, not the WRITE side, which is why AX-based insertion
// (AXUIElementSetAttributeValue on kAXSelectedTextAttribute) only ever worked in
// a handful of apps like TextEdit. Synthetic keyboard events go through the same
// input pipeline as real typing, so they're received correctly almost anywhere.
func typeText(_ text: String) {
    let source = CGEventSource(stateID: .combinedSessionState)
    let utf16Chars = Array(text.utf16)
    guard !utf16Chars.isEmpty else {
        print("{\"success\":true}")
        return
    }

    guard let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
          let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
        print("{\"error\":\"could not create synthetic keyboard event\"}")
        return
    }

    keyDown.keyboardSetUnicodeString(stringLength: utf16Chars.count, unicodeString: utf16Chars)
    keyUp.keyboardSetUnicodeString(stringLength: utf16Chars.count, unicodeString: utf16Chars)

    keyDown.post(tap: .cgSessionEventTap)
    keyUp.post(tap: .cgSessionEventTap)

    print("{\"success\":true}")
}

let args = CommandLine.arguments

if args.count >= 2 && args[1] == "read" {
    readContext()
} else if args.count >= 3 && args[1] == "insert" {
    typeText(args[2])
} else {
    print("{\"error\":\"usage: ax_helper read | ax_helper insert <text>\"}")
}
