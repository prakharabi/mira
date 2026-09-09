import Cocoa

// Posts system media keys (play/pause, next, previous, volume).
//
// Deliberately NOT AppleScript: controlling Music.app or Spotify via Apple
// Events needs an Automation consent grant per target app, and the daemon is a
// headless LaunchAgent that cannot obtain one. Media keys go through the same
// path as the keys on the keyboard itself, so they need no per-app permission
// and they control whatever app currently owns media playback -- Music,
// Spotify, YouTube in a browser, all of it.

let NX_KEYTYPE_SOUND_UP: UInt32 = 0
let NX_KEYTYPE_SOUND_DOWN: UInt32 = 1
let NX_KEYTYPE_MUTE: UInt32 = 7
let NX_KEYTYPE_PLAY: UInt32 = 16
let NX_KEYTYPE_NEXT: UInt32 = 17
let NX_KEYTYPE_PREVIOUS: UInt32 = 18

func postMediaKey(_ key: UInt32) {
    for isDown in [true, false] {
        let flags = NSEvent.ModifierFlags(rawValue: UInt(isDown ? 0xA00 : 0xB00))
        let data1 = Int((key << 16) | UInt32(isDown ? 0xA00 : 0xB00))

        guard let event = NSEvent.otherEvent(
            with: .systemDefined,
            location: .zero,
            modifierFlags: flags,
            timestamp: 0,
            windowNumber: 0,
            context: nil,
            subtype: 8,
            data1: data1,
            data2: -1
        ) else { continue }

        event.cgEvent?.post(tap: .cgSessionEventTap)
    }
    // CGEventPost only enqueues; this process exits immediately after, and
    // without a beat to let the queue drain the key is silently dropped.
    usleep(40000)
}

let map: [String: UInt32] = [
    "playpause": NX_KEYTYPE_PLAY,
    "play": NX_KEYTYPE_PLAY,
    "pause": NX_KEYTYPE_PLAY,
    "next": NX_KEYTYPE_NEXT,
    "previous": NX_KEYTYPE_PREVIOUS,
    "volumeup": NX_KEYTYPE_SOUND_UP,
    "volumedown": NX_KEYTYPE_SOUND_DOWN,
    "mute": NX_KEYTYPE_MUTE,
]

let args = CommandLine.arguments
guard args.count >= 2, let key = map[args[1].lowercased()] else {
    print("{\"error\":\"usage: media_key playpause|next|previous|volumeup|volumedown|mute\"}")
    exit(1)
}

postMediaKey(key)
print("{\"success\":true,\"action\":\"\(args[1].lowercased())\"}")
