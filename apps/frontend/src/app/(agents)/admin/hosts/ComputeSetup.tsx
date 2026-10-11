'use client';

import { useState } from 'react';

import { saveComputeDeploymentProfile } from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import { ErrorNotice, inputClass, primaryClass } from './compute-ui';

const CREDENTIALS = [
  ['gateway.key', 'Inference tunnel private key'],
  ['gateway.known_hosts', 'Inference tunnel verified hosts'],
  ['mcp.key', 'MCP tunnel private key'],
  ['mcp.known_hosts', 'MCP tunnel verified hosts'],
  ['control.key', 'Control tunnel private key'],
  ['control.known_hosts', 'Control tunnel verified hosts'],
] as const;

async function encode(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  const size = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += size) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + size));
  }
  return btoa(binary);
}

export default function ComputeSetup({ onSaved }: { onSaved: () => void }) {
  const [id, setId] = useState('');
  const [label, setLabel] = useState('');
  const [sha, setSha] = useState('');
  const [root, setRoot] = useState('/opt/cloud-agent');
  const [environment, setEnvironment] = useState('');
  const [bundle, setBundle] = useState<File | null>(null);
  const [files, setFiles] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!bundle || busy) return;
    setBusy(true);
    setError(null);
    try {
      await saveComputeDeploymentProfile({
        id,
        label,
        deployment_sha: sha,
        deploy_root: root,
        environment,
        files: Object.fromEntries(
          CREDENTIALS.map(([name]) => [`.compute-secrets/${name}`, files[name] ?? '']),
        ),
        source_bundle_base64: await encode(bundle),
        install_prerequisites: true,
        timeout_seconds: 3600,
      });
      onSaved();
    } catch (reason) {
      setError(getErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <p className="text-sm text-gray-600 dark:text-gray-300">
        Register the reviewed release and the credentials used to install an isolated Host. Secrets
        are encrypted before storage and are never returned by the API.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="text-sm">
          <span className="mb-1 block font-medium">Profile ID</span>
          <input
            className={inputClass}
            value={id}
            pattern="[a-z][a-z0-9_-]{0,63}"
            required
            placeholder="primary-kata"
            onChange={(event) => setId(event.target.value)}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block font-medium">Display name</span>
          <input
            className={inputClass}
            value={label}
            required
            placeholder="Primary Kata host"
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <label className="text-sm sm:col-span-2">
          <span className="mb-1 block font-medium">Reviewed release revision</span>
          <input
            className={inputClass}
            value={sha}
            pattern="[a-f0-9]{40}"
            required
            placeholder="40-character Git commit"
            onChange={(event) => setSha(event.target.value)}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block font-medium">Remote install directory</span>
          <input
            className={inputClass}
            value={root}
            required
            onChange={(event) => setRoot(event.target.value)}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block font-medium">Release bundle</span>
          <input
            className={inputClass}
            type="file"
            accept=".tar,.tar.gz,.tgz,application/gzip"
            required
            onChange={(event) => setBundle(event.target.files?.[0] ?? null)}
          />
        </label>
      </div>
      <label className="block text-sm">
        <span className="mb-1 block font-medium">Host environment</span>
        <textarea
          className={inputClass}
          rows={14}
          value={environment}
          required
          spellCheck={false}
          placeholder="Paste the completed compute-host.env template"
          onChange={(event) => setEnvironment(event.target.value)}
        />
      </label>
      <details>
        <summary className="cursor-pointer text-sm font-medium">Tunnel credentials</summary>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {CREDENTIALS.map(([name, title]) => (
            <label className="text-sm" key={name}>
              <span className="mb-1 block font-medium">{title}</span>
              <textarea
                className={inputClass}
                rows={name.endsWith('.key') ? 7 : 4}
                required
                spellCheck={false}
                value={files[name] ?? ''}
                onChange={(event) =>
                  setFiles((current) => ({ ...current, [name]: event.target.value }))
                }
              />
            </label>
          ))}
        </div>
      </details>
      {error && <ErrorNotice error={error} />}
      <div className="flex justify-end border-t border-gray-200 pt-4 dark:border-white/10">
        <button className={primaryClass} disabled={busy || !bundle} type="submit">
          {busy ? 'Encrypting and saving…' : 'Save server setup'}
        </button>
      </div>
    </form>
  );
}
