/** idle; loading the voice; listening: the mic records you until Done; transcribing; thinking; speaking. */
export type VoicePhase = 'idle' | 'loading' | 'listening' | 'transcribing' | 'thinking' | 'speaking'
/** live: Claude answers when you have finished talking (Smart Turn); hold: when you press Done. */
export type ListenMode = 'live' | 'hold'
/** Live mode: how long a pause runs before Claude answers; long2-long4 are for long thinking pauses. */
export type Patience = 'short' | 'normal' | 'long' | 'long2' | 'long3' | 'long4' | 'long5' | 'long6' | 'long7' | 'long8' | 'long9' | 'long10'
/** The speech settings the gear pane edits, as ~/.claudevoice/config.json holds them. */
export type VoiceSettings = Record<string, string>
/** One chat's own choices, each overriding the all-chats setting; a missing key follows all chats.
 *  Values are the settings pane's option values (speaking "kokoro:<voice>", speed "1.05"...). */
export type ChatSettings = {
  plugin?: string
  listenMode?: string
  patience?: string
  talkOver?: string
  listening?: string
  microphone?: string
  speaking?: string
  speed?: string
}

declare module 'claude-code' {
  interface PluginState {
    'claudevoice': {
      phase: VoicePhase
      isCall: boolean
      heard: string
      note: string
      settings: VoiceSettings
      enabled: boolean
      chat: ChatSettings
      chatOpen: Record<string, boolean>
      tick: number
      muted: boolean
      take: string
      hearing: string
      listenMode: ListenMode
      talkOver: boolean
      patience: Patience
    }
  }
}
