import Cocoa
import ApplicationServices

// Simple state flag: does Node currently have a suggestion showing?
// Controlled via a tiny local HTTP server on 127.0.0.1:11201
var suggestionActive = false

// A swallowed keyDown must have its keyUp swallowed too. Tapping only keyDown
// let a Tab keyUp with no matching keyDown reach the focused app, and that
// stray event raced the synthetic text insertion that follows -- the accepted
// word intermittently never appeared even though posting reported success.
var swallowedTabDown = false

let kVK_Tab: Int64 = 48

func eventTapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent, refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    let keyCode = event.getIntegerValueField(.keyboardEventKeycode)

    if type == .keyDown {
        if keyCode == kVK_Tab && suggestionActive {
            // swallow Tab: notify Node via stdout, don't pass the event through
            swallowedTabDown = true
            print("TAB_PRESSED")
            fflush(stdout)
            return nil
        }
    } else if type == .keyUp {
        // Only the keyUp partnered with a swallowed keyDown; a Tab pressed
        // while no suggestion was showing must still behave completely normally.
        if keyCode == kVK_Tab && swallowedTabDown {
            swallowedTabDown = false
            return nil
        }
    }

    return Unmanaged.passRetained(event)
}

func startEventTap() {
    let eventMask = (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.keyUp.rawValue)

    guard let tap = CGEvent.tapCreate(
        tap: .cgSessionEventTap,
        place: .headInsertEventTap,
        options: .defaultTap,
        eventsOfInterest: CGEventMask(eventMask),
        callback: eventTapCallback,
        userInfo: nil
    ) else {
        FileHandle.standardError.write("Failed to create event tap. Check Accessibility permission.\n".data(using: .utf8)!)
        exit(1)
    }

    let runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), runLoopSource, .commonModes)
    CGEvent.tapEnable(tap: tap, enable: true)
}

// Read state toggle commands from stdin, one per line: "ACTIVE" or "INACTIVE"
func startStdinListener() {
    DispatchQueue.global(qos: .background).async {
        while let line = readLine() {
            if line == "ACTIVE" {
                suggestionActive = true
            } else if line == "INACTIVE" {
                suggestionActive = false
            }
        }
    }
}

startStdinListener()
startEventTap()
CFRunLoopRun()
