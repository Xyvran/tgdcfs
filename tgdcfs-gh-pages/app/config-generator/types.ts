// Shared shapes of the config generator's form state.

export type Backend = "telegram" | "discord";

export interface StoreConfig {
  // Name used inside the config only; never reaches the metadata.
  name: string;
  backend: Backend;
  channel: string;
}

export type MetadataType = "pinned_message" | "github_repo";
export type MirrorMode = "auto" | "forward" | "reupload";
export type SyncMode = "inline" | "background";
// When a write is answered: once the primary store has it, or once the
// local cache has it and a worker moves it into the stores (write-back).
export type WriteAck = "primary" | "cache";

export interface FilesystemConfig {
  name: string;
  primary: string;
  mirrors: string[];
  mode: MirrorMode;
  sync: SyncMode;
  strict: boolean;
  write_ack: WriteAck;
  // Multi-source reads: spread one download over every store that holds
  // the version; read_sources limits the stores that take part.
  read_parallel: boolean;
  read_sources: string[];
  allow_shared_store: boolean;
  metadata: {
    type: MetadataType;
    github_repo: {
      repo: string;
      commit: string;
      access_token: string;
    };
  };
}

export interface DiscordConfig {
  enabled: boolean;
  bot_tokens: string[];
  max_file_size_bytes: number;
  delete_messages_on_remove: boolean;
}

export const isValidDirectoryName = (name: string): boolean => {
  // Valid directory name: no / \ : * ? " < > | and not . or ..
  const invalidChars = /[\/\\:*?"<>|]/;
  return (
    !invalidChars.test(name) &&
    name !== "." &&
    name !== ".." &&
    name.trim().length > 0
  );
};

export const isValidStoreName = (name: string): boolean =>
  /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(name);

// Whether some mirror of the file system can only be filled by
// re-uploading the bytes: another backend, Discord on either side (no
// server-side copy) or an explicit re-upload mode. Mirrors the loader's
// rule for the default sync mode.
export const needsReupload = (
  filesystem: FilesystemConfig,
  stores: StoreConfig[]
): boolean => {
  if (filesystem.mirrors.length === 0) return false;
  if (filesystem.mode === "reupload") return true;
  const primary = stores.find((s) => s.name === filesystem.primary);
  if (!primary) return false;
  return filesystem.mirrors.some((name) => {
    const store = stores.find((s) => s.name === name);
    return (
      store !== undefined &&
      (store.backend === "discord" || store.backend !== primary.backend)
    );
  });
};
