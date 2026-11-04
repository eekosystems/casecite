export interface Connector {
  id: string;
  name: string;
  icon: string;
  color: string;
  connected: boolean;
  configured?: boolean;
  /** Uses one firm-wide credential, so only administrators may connect or sync it. */
  adminOnly?: boolean;
  /** False when the server did not list this connector at all (e.g. an
   *  experimental one that is switched off), so it must not read as configured. */
  available?: boolean;
  /** Implemented against the vendor's API but not validated against a live tenant. */
  experimental?: boolean;
  /** A sync can be scoped to one folder (the folder picker is offered). */
  supportsFolderSync?: boolean;
  docs: number;
}

export interface PickerConfig {
  provider?: string;
  token?: string;
  clientId?: string;
  appKey?: string;
  google_enabled?: boolean;
  /** False when the picker cannot open; `google_missing` names the absent
      settings fields (e.g. ["google_api_key"]). */
  google_configured?: boolean;
  google_missing?: string[];
  google_client_id?: string;
  google_api_key?: string;
  google_app_id?: string;
  microsoft_enabled?: boolean;
  microsoft_client_id?: string;
  microsoft_tenant_id?: string;
  box_enabled?: boolean;
  dropbox_enabled?: boolean;
  dropbox_app_key?: string;
}

export interface PickerFile {
  id: string;
  name: string;
  mimeType?: string;
  size?: number;
  url?: string;
  /** Provider OAuth token scoped to the picked files (Google/OneDrive/Box). */
  accessToken?: string;
  oauthToken?: string;
}
