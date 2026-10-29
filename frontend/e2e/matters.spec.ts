import { test, expect } from '@playwright/test';
import { loginAs, fulfillJson, TEST_USER, type MockDocument } from './helpers/auth';

// The matter workspace against a mocked API: the list, one matter's tabs, and
// the header selector that scopes the other views to the active matter.

const PERSONAL = {
  id: 'matter-personal', name: 'Personal', client_name: null, is_personal: true,
  owner_id: TEST_USER.id, role: 'owner', member_count: 1, created_at: '2026-09-01T09:00:00Z',
};
const SHARED = {
  id: 'matter-acme', name: 'Acme v. Beta', client_name: 'Acme Corp', is_personal: false,
  owner_id: TEST_USER.id, role: 'owner', member_count: 2, created_at: '2026-10-01T09:00:00Z',
};
const MEMBERS = [
  { user_id: TEST_USER.id, email: TEST_USER.email, name: TEST_USER.name, role: 'owner' },
  { user_id: 'user-2', email: 'sam.lee@example.com', name: 'Sam Lee', role: 'member' },
];
const FILED_DOC: MockDocument & { user_id: string; matter_id: string } = {
  id: 'doc-7', filename: 'master-services-agreement.pdf', content_type: 'application/pdf',
  size: 48_000, source: 'upload', status: 'indexed', chunk_count: 12,
  created_at: '2026-10-02T09:00:00Z', folder_path: null, user_id: 'user-2', matter_id: SHARED.id,
};

test.describe('Matters', () => {
  test.use({ viewport: { width: 1280, height: 1000 } });

  test('lists matters, opens a workspace and walks its tabs', async ({ page }) => {
    await loginAs(page, '/matters', {
      overrides: [
        async (route, url, method) => {
          const path = url.pathname.replace(/^\/api\/v1/, '');
          if (method !== 'GET') return false;
          if (path === '/matters') { await fulfillJson(route, 200, { matters: [PERSONAL, SHARED] }); return true; }
          if (path === `/matters/${SHARED.id}`) { await fulfillJson(route, 200, { ...SHARED, members: MEMBERS }); return true; }
          if (path === `/matters/${SHARED.id}/documents`) {
            await fulfillJson(route, 200, { documents: [FILED_DOC], count: 1, total: 1, limit: null, offset: 0 });
            return true;
          }
          if (path === `/matters/${SHARED.id}/chats`) {
            await fulfillJson(route, 200, { sessions: [{
              id: 's1', title: 'Liability cap exposure', message_count: 4, user_id: 'user-2', matter_id: SHARED.id,
              created_at: '2026-10-03T09:00:00Z', updated_at: '2026-10-03T10:00:00Z',
            }] });
            return true;
          }
          if (path === `/matters/${SHARED.id}/analyses`) {
            await fulfillJson(route, 200, { analyses: [{
              analysis_id: 'a1', document_id: FILED_DOC.id, filename: FILED_DOC.filename,
              contract_type: 'msa', created_at: '2026-10-04T09:00:00Z', issues: 3, user_id: TEST_USER.id,
            }] });
            return true;
          }
          if (path === `/matters/${SHARED.id}/activity`) {
            await fulfillJson(route, 200, { events: [
              { id: 2, timestamp: '2026-10-02T09:05:00Z', event_type: 'data_modification', action: 'matter_member_added',
                user_id: TEST_USER.id, user_email: 'j***@example.com', resource_type: 'matter', resource_id: SHARED.id, success: true },
              { id: 1, timestamp: '2026-10-01T09:00:00Z', event_type: 'data_modification', action: 'matter_created',
                user_id: TEST_USER.id, user_email: 'j***@example.com', resource_type: 'matter', resource_id: SHARED.id, success: true },
            ] });
            return true;
          }
          return false;
        },
      ],
    });

    const main = page.locator('main[data-layout="main-content"]');
    await expect(main.getByRole('heading', { name: 'Matters' })).toBeVisible();
    await expect(main.getByText('Acme v. Beta', { exact: true })).toBeVisible();
    await expect(main.getByText('Acme Corp', { exact: true })).toBeVisible();
    await expect(main.getByText('2 members')).toBeVisible();

    // The header selector lists the same matters.
    const select = page.getByLabel('Matter', { exact: true });
    await expect(select).toBeVisible();
    await expect(select.locator('option')).toHaveText(['All matters', 'Personal', 'Acme v. Beta — Acme Corp']);

    // Open the shared matter's workspace.
    await main.getByText('Acme v. Beta', { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/matters/${SHARED.id}$`));
    await expect(main.getByRole('heading', { name: 'Acme v. Beta' })).toBeVisible();
    await expect(main.getByText('Client: Acme Corp')).toBeVisible();

    // Documents tab is the default and labels a colleague's document.
    await expect(main.getByText(FILED_DOC.filename)).toBeVisible();
    await expect(main.getByText('Colleague')).toBeVisible();

    await main.getByRole('tab', { name: 'Chats' }).click();
    await expect(main.getByText('Liability cap exposure')).toBeVisible();
    await expect(main.getByText('4 messages')).toBeVisible();

    await main.getByRole('tab', { name: 'Analyses' }).click();
    await expect(main.getByText('3 issues')).toBeVisible();

    await main.getByRole('tab', { name: 'Members' }).click();
    await expect(main.getByText('Sam Lee')).toBeVisible();
    await expect(main.getByRole('button', { name: 'Make owner' })).toBeVisible();
    await expect(main.getByRole('button', { name: 'Remove' })).toBeVisible();

    await main.getByRole('tab', { name: 'Activity' }).click();
    await expect(main.getByText('Matter member added')).toBeVisible();
    await expect(main.getByText('Matter created')).toBeVisible();

    // Set it as the active matter: the header selector follows and remembers it.
    await main.getByRole('button', { name: 'Set as active' }).click();
    await expect(select).toHaveValue(SHARED.id);
    await expect(main.getByText('Active matter', { exact: true })).toBeVisible();
    await expect(main.getByRole('button', { name: 'Clear active matter' })).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('casecite.activeMatterId'))).toBe(SHARED.id);
  });

  test('a matter the user cannot see answers with a clear message', async ({ page }) => {
    await loginAs(page, '/matters/not-mine');
    const main = page.locator('main[data-layout="main-content"]');
    await expect(main.getByText(/not a member/)).toBeVisible();
    await expect(main.getByRole('button', { name: 'All matters' })).toBeVisible();
  });
});
