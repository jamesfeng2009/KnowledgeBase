/// <reference path="../.astro/types.d.ts" />

interface ImportMetaEnv {
  readonly PUBLIC_API_BASE: string;
  readonly PUBLIC_WS_URL: string;
  readonly PUBLIC_APP_NAME: string;
  readonly PUBLIC_APP_ENV: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
