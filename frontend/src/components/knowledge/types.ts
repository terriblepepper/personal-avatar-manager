import type { StorageQuota } from '../../utils/format'

export interface Asset { id: string; filename: string; caption: string; mime?: string; size?: number; created_at?: string; source_attachment_id?: string; relative_path?: string; download_url?: string }
export interface Question { id: string; category: string; category_name: string; category_index: number; question: string }
export interface Answer { id: string; choice: string; other: string }
export interface Turn { question?: string; answer?: string | null }
export interface Build { id: string; status: string; origin: string; prompt: string; updated_at: string; created_at: string; stage: string; progress: number; error?: string; reason?: string; phase: string; document: Record<string,string>; question_plan: Question[]; bank_answers: Record<string,{choice:string;other:string}>; interview: Turn[]; asset_ids: string[]; interview_rounds: number }
export interface Knowledge { quota?: StorageQuota; assets: Asset[]; builds: Build[]; active_build_id: string | null; question_categories: unknown[]; model: {model:string;vision_enabled:boolean;audio_enabled:boolean}; learning?: {status:string;error?:string}; folders?: {error?:string;pending?:boolean}; last_prompt?: string }
export interface MemoryNode { id:string; group:string; title:string; summary:string; content:string; deleted?:boolean }
export interface MemoryTree { revision:string; nodes:MemoryNode[] }
