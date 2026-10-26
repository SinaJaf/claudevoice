export type VoicePhase = 'idle' | 'loading' | 'listening' | 'thinking' | 'speaking'
/** auto: a pause ends your turn. hold: you press Done when you have finished. */
export type ListenMode = 'auto' | 'hold'
/** The speech settings the gear pane edits, as ~/.claudevoice/config.json holds them. */
export type VoiceSettings = Record<string, string>
/** One chat's own choices, each overriding the all-chats setting; a missing key follows all chats.
 *  Values are the settings pane's option values (listening "local:<repo>", speaking "kokoro:<voice>"...). */
export type ChatSettings = {
  plugin?: string
  listening?: string
  microphone?: string
  speaking?: string
  speed?: string
  listenMode?: string
}

declare module 'claude-code' {
  interface PluginState {
    'claudevoice': {
      phase: VoicePhase
      isCall: boolean
      heard: string
      note: string
      mode: ListenMode
      settings: VoiceSettings
      enabled: boolean
      /** In a call: the microphone records nothing until unmuted. */
      muted: boolean
      chat: ChatSettings
      chatOpen: Record<string, boolean>
    }
  }
}
