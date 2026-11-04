import { readFileSync } from 'node:fs';
import { defineConfig, type ServerOptions } from 'vite';
import react from '@vitejs/plugin-react';
import basicSsl from '@vitejs/plugin-basic-ssl';

/**
 * Office loads a task pane over HTTPS only, localhost included. Point
 * ADDIN_SSL_KEY / ADDIN_SSL_CERT at a certificate Word already trusts (for
 * example the one `npx office-addin-dev-certs install` writes under
 * ~/.office-addin-dev-certs); without them the basic-ssl plugin serves a
 * self-signed certificate that must be trusted once in the OS.
 */
const keyPath = process.env.ADDIN_SSL_KEY;
const certPath = process.env.ADDIN_SSL_CERT;
const haveOwnCert = Boolean(keyPath && certPath);

const https: ServerOptions['https'] | undefined = haveOwnCert
  ? { key: readFileSync(keyPath as string), cert: readFileSync(certPath as string) }
  : undefined;

export default defineConfig({
  // The Docker image serves the pane at /addin/ on the app's own origin
  // (ADDIN_BASE=/addin/); a standalone host serves it at its root.
  base: process.env.ADDIN_BASE || '/',
  plugins: [react(), ...(haveOwnCert ? [] : [basicSsl()])],
  server: {
    port: Number(process.env.ADDIN_PORT || 3200),
    strictPort: true,
    https,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
