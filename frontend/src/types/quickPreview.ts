export interface QuickPreviewFile {
  path: string;
  previous_path: string | null;
  status: string;
  additions: number;
  deletions: number;
}
export interface QuickPreview {
  pr_url: string;
  repository: string;
  number: number;
  title: string;
  head_sha: string;
  declared_base_sha: string;
  files: QuickPreviewFile[];
  files_complete: boolean;
  requires_full_preview: boolean;
}
