/** idle; loading the voice; listening: your turn to talk; thinking: Claude is working; speaking. */
export type VoicePhase = 'idle' | 'loading' | 'listening' | 'thinking' | 'speaking'
/** The speech settings the gear pane edits, as ~/.claudevoice/config.json holds them. */
export type VoiceSettings = Record<string, string>
/** One chat's own choices, each overriding the all-chats setting; a missing key follows all chats.
 *  Values are the settings pane's option values (speaking "kokoro:<voice>", speed "1.05"...). */
export type ChatSettings = {
  plugin?: string
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
    }
  }
}
