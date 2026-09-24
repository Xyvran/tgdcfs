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

export interface FilesystemConfig {
  name: string;
  primary: string;
  mirrors: string[];
  mode: MirrorMode;
  sync: SyncMode;
  strict: boolean;
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
