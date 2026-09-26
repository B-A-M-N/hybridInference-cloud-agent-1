import { getAgentIntegrations } from '@/lib/api/agents';

/**
 * Where GitHub takes the owner for one step, or `null` when this deployment
 * cannot send them there.
 *
 * `connect` authorizes the App as the owner. `manage` is the App's installation
 * page, where GitHub asks which account — the owner's own or an organization's
 * — and which of its repositories the App may use; it is the way to add a
 * repository, and it falls back to connecting for someone who has not.
 *
 * Asked for at the moment of leaving, not kept from page load: each link
 * carries a single-use state that expires in minutes.
 */
export async function githubAccessUrl(kind: 'connect' | 'manage'): Promise<string | null> {
  const { providers } = await getAgentIntegrations();
  const github = providers.find((provider) => provider.provider === 'github');
  if (!github) return null;
  return (kind === 'manage' ? github.manage_url : null) ?? github.connect_url ?? null;
}

/** Leave for GitHub. Its own function so a test can stand in for the browser. */
export function leaveFor(url: string): void {
  window.location.assign(url);
}
