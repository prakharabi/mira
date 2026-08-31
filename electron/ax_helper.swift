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

    print("{\"textBeforeCursor\":\"\(escaped)\",\"caretX\":\(caretX),\"caretY\":\(caretY),\"boundsError\":\(boundsErrorCode)}")
}

func insertText(_ newText: String) {
    guard let el = getFocusedElement() else {
        print("{\"error\":\"no focused element\"}")
        return
    }

    var selectedRange: AnyObject?
    let rangeResult = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute as CFString, &selectedRange)

    guard rangeResult == .success, let rangeValue = selectedRange else {
        print("{\"error\":\"could not get selection range\"}")
        return
    }

    let setResult = AXUIElementSetAttributeValue(el, kAXSelectedTextAttribute as CFString, newText as CFTypeRef)

    if setResult == .success {
        print("{\"success\":true}")
    } else {
        print("{\"error\":\"insert failed\",\"code\":\(setResult.rawValue)}")
    }
}

let args = CommandLine.arguments

if args.count >= 2 && args[1] == "read" {
    readContext()
} else if args.count >= 3 && args[1] == "insert" {
    insertText(args[2])
} else {
    print("{\"error\":\"usage: ax_helper read | ax_helper insert <text>\"}")
}
