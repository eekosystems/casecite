export interface RagDocument {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  source: string;
  source_id?: string | null;
  status: 'pending' | 'processing' | 'indexed' | 'failed';
  chunk_count: number;
  created_at: string;
  indexed_at?: string | null;
  metadata?: Record<string, unknown>;
  folder_path?: string | null;
  /** Owner of the document (a matter member may see colleagues' documents). */
  user_id?: string;
  /** Matter the document is filed under; null = the owner's personal scope. */
  matter_id?: string | null;
}

export interface DocumentFilter {
  document_ids?: string[] | null;
  folder_paths?: string[] | null;
  sources?: string[] | null;
  doc_types?: string[] | null;
  include_subfolders?: boolean;
  search_all?: boolean;
  /** Matters whose documents to include; the server resolves them (membership-checked). */
  matter_ids?: string[] | null;
}

export interface DocumentTreeItem {
  id: string;
  name: string;
  type: 'file' | 'folder';
  path: string;
  source?: string | null;
  doc_type?: string | null;
  size?: number | null;
  children: DocumentTreeItem[];
  document_count: number;
}

export interface DocumentTree {
  tree: DocumentTreeItem[];
  total_documents: number;
  [key: string]: unknown;
}

export interface UploadedDocument {
  id: string;
  name: string;
  size: number;
  type: string;
  uploadedAt: Date;
  status: 'uploading' | 'indexed' | 'error';
  file?: File;
  serverId?: string;
  chunks?: number;
  error?: string;
  indexed_at?: string;
}
