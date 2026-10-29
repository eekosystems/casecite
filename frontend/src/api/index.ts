// Barrel re-export — all domain modules attach methods to the shared api singleton.
// Consumers continue to import { api, API_BASE_URL } from '../api' unchanged.
import { api, API_BASE_URL } from './client';
import './auth';
import './mfa';
import './deviceAuth';
import './chat';
import './chatSessions';
import './workspaceSessions';
import './documents';
import './matters';
import './reviewTables';
import './deepResearch';
import './workflows';
import './settings';
import './connectors';
import './pickers';
import './cases';
import './contracts';
import './integrations';
import './users';
import './branding';

export { api, API_BASE_URL };
