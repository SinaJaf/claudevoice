export type VoicePhase = 'idle' | 'loading' | 'listening' | 'thinking' | 'speaking'
/** auto: a pause ends your turn. hold: you press Done when you have finished. */
export type ListenMode = 'auto' | 'hold'
/** The speech settings the gear pane edits, as ~/.claudevoice/config.json holds them. */
export type VoiceSettings = Record<string, string>

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
    }
  }
}
