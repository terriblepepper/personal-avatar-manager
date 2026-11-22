export interface User {
  id: string
  username: string
  role: 'admin' | 'user'
  person_type: 'student' | 'teacher'
  account_status: string
  is_root?: boolean | number
  onboarding_stage?: 'welcome' | 'tour' | 'done'
  access_scope?: 'full' | 'profile'
  auth_source?: string
  profile_name?: string
  identity_username?: string
  student_id?: string
  email?: string
  phone?: string
  account_reason?: string
}
export interface AuthConfig { version?: string; oidc_enabled: boolean; account_url?: string; client_id?: string; issuer?: string; runtime_mode?: string }
export interface SessionResponse { user: User; csrf: string }
export interface Page<T> { items: T[]; total: number; offset: number }
export interface AnimationMetadata {
  title?: string
  tags?: string[]
  durationMs?: number
}

export interface FilePromotion {
  new_name: string
  caption: string
  visibility: 'private' | 'public'
  folder?: string
}

export interface ChatFile {
  id: string; filename: string; content_type?: string; size?: number; referenced?: boolean
  origin?: string; promoted_id?: string | null; expires_at?: string; expired?: boolean
  caption?: string; download_url?: string; visibility?: string; temporary?: boolean
}
export interface ToolEvent { id: string; name: string; status: string; group?: string; phase?: string; input?: unknown; output?: unknown; steps?: { time: string; message: string }[] }
export interface SkillSummary { id: string; name: string; description?: string }
export interface ChatMessage {
  id: number; role: string; content: string; created_at: string; responder_name?: string
  attachments?: ChatFile[]; used_skills?: SkillSummary[]; tool_events?: ToolEvent[]; summary_through?: number
  task_event?: Record<string, unknown>; intent?: Record<string, unknown>
}
export interface AgentQuestion { id: string; question: string; options?: string[] }
export interface QuestionReply { question_id: string; message: string; request_id: string }
export interface GenerationRequest { question_id?: string; request_id: string; conversation_id: string; message: string; personal: boolean; channel: 'assistant' | 'avatar'; attachment_ids: string[]; agent_pause_id?: string; agent_control?: 'continue' | 'retry' | 'end' }
export interface GenerationSnapshot {
  user_question?: AgentQuestion
  request_id?: string; conversation_id?: string; state?: string; message?: string; agent_pause_id?: string; stream_retryable?: boolean
  reply: string; status?: string; stage?: string; done: boolean; error?: string
  tool_events?: ToolEvent[]; used_skills?: SkillSummary[]; mermaid_enabled?: boolean
  target?: { name: string; aic: string }; usage?: Record<string, number>; result?: Record<string, unknown> & { generated_files?: ChatFile[] }
}
