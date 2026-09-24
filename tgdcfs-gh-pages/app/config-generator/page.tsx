"use client";

import { Add, ContentCopy, Download, Refresh } from "@mui/icons-material";
import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  Container,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Typography,
} from "@mui/material";
import yaml from "js-yaml";
import { useCallback, useEffect, useState } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";
import { BotTokenField } from "./components/BotTokenField";
import { ConfigTextField } from "./components/ConfigTextField";
import {
  EncryptionConfig,
  EncryptionField,
} from "./components/EncryptionField";
import { FieldRow } from "./components/FieldRow";
import { FilesystemField } from "./components/FilesystemField";
import { FormSection } from "./components/FormSection";
import { StoreField } from "./components/StoreField";
import { UserField } from "./components/UserField";
import {
  DiscordConfig,
  FilesystemConfig,
  StoreConfig,
  isValidDirectoryName,
  isValidStoreName,
} from "./types";

interface SftpConfig {
  enabled: boolean;
  host: string;
  port: number;
  host_key_file: string;
  authorized_keys_dir: string;
  upload_buffer_size_mb: number;
}

interface TransferConfig {
  // UI only: when off, no transfer block is written at all and the
  // application falls back to its own defaults.
  enabled: boolean;
  upload_workers_small: number;
  upload_workers_big: number;
  upload_part_size_kb: number;
  download_piece_size_kb: number;
  download_pieces_in_flight: number;
  parallel_download_threshold_mb: number;
  connection_pool_size: number;
  chunk_cache_mb: number;
  chunk_cache_readahead: number;
  chunk_cache_block_kb: number;
}

interface ConfigData {
  telegram: {
    api_id: string;
    api_hash: string;
    lib: "pyrogram" | "telethon";
    account: {
      session_file: string;
    };
    bot: {
      session_file: string;
      tokens: string[];
    };
  };
  discord: DiscordConfig;
  stores: StoreConfig[];
  filesystems: FilesystemConfig[];
  tgdcfs: {
    users: {
      username: string;
      password: string;
    }[];
    jwt: {
      secret: string;
      algorithm: string;
      life: number;
    };
    server: {
      host: string;
      port: number;
    };
    sftp: SftpConfig;
    transfer: TransferConfig;
    encryption: EncryptionConfig;
  };
}

// Type-safe path mapping for updateConfig
type ConfigUpdatePaths = {
  "telegram.api_id": string;
  "telegram.api_hash": string;
  "telegram.lib": "pyrogram" | "telethon";
  "telegram.bot.tokens": string[];
  discord: DiscordConfig;
  stores: StoreConfig[];
  filesystems: FilesystemConfig[];
  "tgdcfs.users": { username: string; password: string }[];
  "tgdcfs.jwt.secret": string;
  "tgdcfs.jwt.algorithm": string;
  "tgdcfs.jwt.life": number;
  "tgdcfs.server.host": string;
  "tgdcfs.server.port": number;
  "tgdcfs.sftp": SftpConfig;
  "tgdcfs.transfer": TransferConfig;
  "tgdcfs.encryption": EncryptionConfig;
};

const generateRandomSecret = (): string => {
  const chars =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*()_+-=[]{}|;:,.<>?";
  let result = "";
  for (let i = 0; i < 64; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
};

const newFilesystem = (name: string, primary: string): FilesystemConfig => ({
  name,
  primary,
  mirrors: [],
  mode: "auto",
  sync: "inline",
  strict: false,
  allow_shared_store: false,
  metadata: {
    type: "pinned_message",
    github_repo: { repo: "", commit: "master", access_token: "" },
  },
});

export default function ConfigGenerator() {
  const [withUserAccountUpload, setWithUserAccountUpload] = useState(false);
  const [withUserAccountDownload, setWithUserAccountDownload] = useState(false);

  const [config, setConfig] = useState<ConfigData>({
    telegram: {
      api_id: "",
      api_hash: "",
      lib: "telethon",
      account: {
        session_file: "account.session",
      },
      bot: {
        session_file: "bot.session",
        tokens: [""],
      },
    },
    discord: {
      enabled: false,
      bot_tokens: [""],
      max_file_size_bytes: 10000000,
      delete_messages_on_remove: false,
    },
    stores: [{ name: "tg-main", backend: "telegram", channel: "" }],
    filesystems: [newFilesystem("default", "tg-main")],
    tgdcfs: {
      users: [
        {
          username: "user",
          password: "password",
        },
      ],
      jwt: {
        secret: "",
        algorithm: "HS256",
        life: 604800,
      },
      server: {
        host: "0.0.0.0",
        port: 1900,
      },
      sftp: {
        enabled: false,
        host: "0.0.0.0",
        port: 2222,
        host_key_file: "sftp_host_key",
        authorized_keys_dir: "",
        upload_buffer_size_mb: 64,
      },
      transfer: {
        enabled: false,
        upload_workers_small: 3,
        upload_workers_big: 8,
        upload_part_size_kb: 512,
        download_piece_size_kb: 4096,
        download_pieces_in_flight: 4,
        parallel_download_threshold_mb: 10,
        connection_pool_size: 1,
        chunk_cache_mb: 0,
        chunk_cache_readahead: 2,
        chunk_cache_block_kb: 1024,
      },
      encryption: {
        enabled: false,
        encrypt_names: false,
        passphrase_source: "passphrase_env",
        passphrase: "",
        passphrase_env: "TGDCFS_MASTER_PASSPHRASE",
        passphrase_file: "secrets/master.passphrase",
        master_salt_file: "master.salt",
        chunk_size: 65536,
      },
    },
  });

  const updateConfig = useCallback(
    <K extends keyof ConfigUpdatePaths>(
      path: K,
      value: ConfigUpdatePaths[K]
    ): void => {
      const newConfig = { ...config };

      if (path === "telegram.api_id") {
        newConfig.telegram.api_id = value as string;
      } else if (path === "telegram.api_hash") {
        newConfig.telegram.api_hash = value as string;
      } else if (path === "telegram.lib") {
        newConfig.telegram.lib = value as "pyrogram" | "telethon";
      } else if (path === "telegram.bot.tokens") {
        newConfig.telegram.bot.tokens = value as string[];
      } else if (path === "discord") {
        newConfig.discord = value as DiscordConfig;
      } else if (path === "stores") {
        newConfig.stores = value as StoreConfig[];
      } else if (path === "filesystems") {
        newConfig.filesystems = value as FilesystemConfig[];
      } else if (path === "tgdcfs.users") {
        newConfig.tgdcfs.users = value as {
          username: string;
          password: string;
        }[];
      } else if (path === "tgdcfs.jwt.secret") {
        newConfig.tgdcfs.jwt.secret = value as string;
      } else if (path === "tgdcfs.jwt.algorithm") {
        newConfig.tgdcfs.jwt.algorithm = value as string;
      } else if (path === "tgdcfs.jwt.life") {
        newConfig.tgdcfs.jwt.life = value as number;
      } else if (path === "tgdcfs.server.host") {
        newConfig.tgdcfs.server.host = value as string;
      } else if (path === "tgdcfs.server.port") {
        newConfig.tgdcfs.server.port = value as number;
      } else if (path === "tgdcfs.sftp") {
        newConfig.tgdcfs.sftp = value as SftpConfig;
      } else if (path === "tgdcfs.transfer") {
        newConfig.tgdcfs.transfer = value as TransferConfig;
      } else if (path === "tgdcfs.encryption") {
        newConfig.tgdcfs.encryption = value as EncryptionConfig;
      }

      setConfig(newConfig);
    },
    [config]
  );

  // Generate JWT secret on client side only to avoid hydration mismatch
  useEffect(() => {
    if (config.tgdcfs.jwt.secret === "") {
      updateConfig("tgdcfs.jwt.secret", generateRandomSecret());
    }
  }, [config.tgdcfs.jwt.secret, updateConfig]);

  const telegramUsed = config.stores.some((s) => s.backend === "telegram");

  const addBotToken = () => {
    const newTokens = [...config.telegram.bot.tokens, ""];
    updateConfig("telegram.bot.tokens", newTokens);
  };

  const removeBotToken = (index: number) => {
    const newTokens = config.telegram.bot.tokens.filter((_, i) => i !== index);
    updateConfig("telegram.bot.tokens", newTokens);
  };

  const updateBotToken = (index: number, value: string) => {
    const newTokens = [...config.telegram.bot.tokens];
    newTokens[index] = value;
    updateConfig("telegram.bot.tokens", newTokens);
  };

  const updateDiscord = <K extends keyof DiscordConfig>(
    field: K,
    value: DiscordConfig[K]
  ) => {
    updateConfig("discord", { ...config.discord, [field]: value });
  };

  const updateDiscordToken = (index: number, value: string) => {
    const tokens = [...config.discord.bot_tokens];
    tokens[index] = value;
    updateDiscord("bot_tokens", tokens);
  };

  // -- stores ---------------------------------------------------------------

  const addStore = () => {
    updateConfig("stores", [
      ...config.stores,
      {
        name: `store-${config.stores.length + 1}`,
        backend: config.discord.enabled ? "discord" : "telegram",
        channel: "",
      },
    ]);
  };

  const removeStore = (index: number) => {
    const removed = config.stores[index].name;
    const stores = config.stores.filter((_, i) => i !== index);
    // Drop references to the removed store.
    const filesystems = config.filesystems.map((fs) => ({
      ...fs,
      primary: fs.primary === removed ? "" : fs.primary,
      mirrors: fs.mirrors.filter((m) => m !== removed),
    }));
    setConfig({ ...config, stores, filesystems });
  };

  const updateStore = (
    index: number,
    field: keyof StoreConfig,
    value: string
  ) => {
    const stores = [...config.stores];
    const oldName = stores[index].name;
    stores[index] = { ...stores[index], [field]: value };
    let filesystems = config.filesystems;
    if (field === "name" && oldName !== value) {
      // Keep file systems pointing at the renamed store.
      filesystems = config.filesystems.map((fs) => ({
        ...fs,
        primary: fs.primary === oldName ? value : fs.primary,
        mirrors: fs.mirrors.map((m) => (m === oldName ? value : m)),
      }));
    }
    setConfig({ ...config, stores, filesystems });
  };

  const getStoreNameErrors = (index: number): string[] => {
    const errors: string[] = [];
    const name = config.stores[index].name.trim();
    if (!name) {
      errors.push("Store name is required");
      return errors;
    }
    if (!isValidStoreName(name)) {
      errors.push("Letters, digits, '-', '_' and '.' only");
    }
    if (
      config.stores.findIndex((s, i) => i !== index && s.name.trim() === name) !==
      -1
    ) {
      errors.push("Store names must be unique");
    }
    return errors;
  };

  const getStoreChannelErrors = (index: number): string[] => {
    const errors: string[] = [];
    const store = config.stores[index];
    const channel = store.channel.trim();
    if (!channel) {
      errors.push("Channel ID is required");
      return errors;
    }
    if (store.backend === "discord" && !/^\d+$/.test(channel)) {
      errors.push("Discord channel ids are numeric");
    }
    if (store.backend === "discord" && !config.discord.enabled) {
      errors.push("Enable the Discord backend below");
    }
    if (
      config.stores.findIndex(
        (s, i) =>
          i !== index && s.backend === store.backend && s.channel.trim() === channel
      ) !== -1
    ) {
      errors.push("Another store uses this channel");
    }
    return errors;
  };

  // -- file systems ----------------------------------------------------------

  const addFilesystem = () => {
    updateConfig("filesystems", [
      ...config.filesystems,
      newFilesystem(`filesystem-${config.filesystems.length + 1}`, ""),
    ]);
  };

  const removeFilesystem = (index: number) => {
    updateConfig(
      "filesystems",
      config.filesystems.filter((_, i) => i !== index)
    );
  };

  const updateFilesystem = <K extends keyof FilesystemConfig>(
    index: number,
    field: K,
    value: FilesystemConfig[K]
  ) => {
    const filesystems = [...config.filesystems];
    const fs = { ...filesystems[index], [field]: value };
    if (field === "primary") {
      fs.mirrors = fs.mirrors.filter((m) => m !== value);
    }
    if (field === "sync" && value === "background") {
      fs.strict = false;
    }
    filesystems[index] = fs;
    updateConfig("filesystems", filesystems);
  };

  const updateFilesystemGitHubRepo = (
    index: number,
    field: keyof FilesystemConfig["metadata"]["github_repo"],
    value: string
  ) => {
    const filesystems = [...config.filesystems];
    filesystems[index] = {
      ...filesystems[index],
      metadata: {
        ...filesystems[index].metadata,
        github_repo: {
          ...filesystems[index].metadata.github_repo,
          [field]: value,
        },
      },
    };
    updateConfig("filesystems", filesystems);
  };

  const getFilesystemNameErrors = (index: number): string[] => {
    const errors: string[] = [];
    const name = config.filesystems[index].name;
    if (!name.trim()) {
      errors.push("Name is required");
      return errors;
    }
    if (!isValidDirectoryName(name)) {
      errors.push('Invalid characters. Cannot contain: / \\ : * ? " < > |');
    }
    if (
      config.filesystems.findIndex(
        (fs, i) =>
          i !== index && fs.name.trim().toLowerCase() === name.trim().toLowerCase()
      ) !== -1
    ) {
      errors.push("File system names must be unique");
    }
    return errors;
  };

  const primaryOwner = (storeName: string, exceptIndex: number) =>
    config.filesystems.findIndex(
      (fs, i) => i !== exceptIndex && fs.primary === storeName
    );

  const getFilesystemPrimaryErrors = (index: number): string[] => {
    const fs = config.filesystems[index];
    if (!fs.primary || !config.stores.some((s) => s.name === fs.primary)) {
      return ["Pick a primary store"];
    }
    if (primaryOwner(fs.primary, index) !== -1) {
      return ["This store is already the primary of another file system"];
    }
    return [];
  };

  const filesystemNeedsSharedStore = (index: number): boolean =>
    config.filesystems[index].mirrors.some(
      (m) => primaryOwner(m, index) !== -1
    );

  const getFilesystemMirrorErrors = (index: number): string[] => {
    const fs = config.filesystems[index];
    const errors: string[] = [];
    if (filesystemNeedsSharedStore(index) && !fs.allow_shared_store) {
      errors.push(
        "A mirror is the primary of another file system; allow the shared store below or pick another mirror"
      );
    }
    return errors;
  };

  const generateYaml = () => {
    const stores: {
      [name: string]: { backend: string; channel: string };
    } = {};
    config.stores
      .filter((s) => s.name.trim() !== "" && s.channel.trim() !== "")
      .forEach((s) => {
        stores[s.name.trim()] = { backend: s.backend, channel: s.channel.trim() };
      });

    const filesystems: {
      [name: string]: {
        primary: string;
        mirrors?: string[];
        mode?: string;
        sync?: string;
        strict?: boolean;
        allow_shared_store?: boolean;
        metadata: {
          type: string;
          github_repo?: { repo: string; commit: string; access_token: string };
        };
      };
    } = {};
    config.filesystems
      .filter((fs) => fs.name.trim() !== "" && fs.primary in stores)
      .forEach((fs) => {
        const mirrors = fs.mirrors.filter((m) => m in stores && m !== fs.primary);
        filesystems[fs.name.trim()] = {
          primary: fs.primary,
          ...(mirrors.length > 0
            ? {
                mirrors,
                mode: fs.mode,
                sync: fs.sync,
                strict: fs.strict,
                ...(fs.allow_shared_store ? { allow_shared_store: true } : {}),
              }
            : {}),
          metadata: {
            type: fs.metadata.type,
            ...(fs.metadata.type === "github_repo"
              ? { github_repo: fs.metadata.github_repo }
              : {}),
          },
        };
      });

    const pinnedOnTelegram = config.filesystems.some(
      (fs) =>
        fs.metadata.type === "pinned_message" &&
        config.stores.find((s) => s.name === fs.primary)?.backend === "telegram"
    );

    const configForYaml = {
      backends: {
        ...(telegramUsed
          ? {
              telegram: {
                api_id: config.telegram.api_id,
                api_hash: config.telegram.api_hash,
                lib: config.telegram.lib,
                ...(withUserAccountUpload ||
                withUserAccountDownload ||
                pinnedOnTelegram
                  ? {
                      account: {
                        session_file: "account.session",
                        used_to_upload: withUserAccountUpload,
                        used_to_download: withUserAccountDownload,
                      },
                    }
                  : {}),
                bot: {
                  session_file: config.telegram.bot.session_file,
                  tokens: config.telegram.bot.tokens.filter(
                    (token) => token.trim() !== ""
                  ),
                },
              },
            }
          : {}),
        ...(config.discord.enabled
          ? {
              discord: {
                bot_tokens: config.discord.bot_tokens.filter(
                  (token) => token.trim() !== ""
                ),
                max_file_size_bytes: config.discord.max_file_size_bytes,
                delete_messages_on_remove:
                  config.discord.delete_messages_on_remove,
              },
            }
          : {}),
      },
      stores,
      filesystems,
      tgdcfs: {
        users: config.tgdcfs.users.reduce((acc, user) => {
          if (user.username.trim() !== "") {
            acc[user.username] = { password: user.password };
          }
          return acc;
        }, {} as { [key: string]: { password: string } }),
        jwt: config.tgdcfs.jwt,
        server: config.tgdcfs.server,
        ...(() => {
          const sftp = config.tgdcfs.sftp;
          if (!sftp.enabled) return {};
          const block: {
            enabled: boolean;
            host: string;
            port: number;
            host_key_file: string;
            authorized_keys_dir?: string;
            upload_buffer_size_mb: number;
          } = {
            enabled: true,
            host: sftp.host,
            port: sftp.port,
            host_key_file: sftp.host_key_file,
            upload_buffer_size_mb: sftp.upload_buffer_size_mb,
          };
          if (sftp.authorized_keys_dir.trim() !== "") {
            block.authorized_keys_dir = sftp.authorized_keys_dir.trim();
          }
          return { sftp: block };
        })(),
        ...(() => {
          const transfer = config.tgdcfs.transfer;
          if (!transfer.enabled) return {};
          // "enabled" only drives this form; it is not a config key.
          const settings = { ...transfer } as Partial<TransferConfig>;
          delete settings.enabled;
          return { transfer: settings };
        })(),
        encryption: (() => {
          const enc = config.tgdcfs.encryption;
          const block: {
            enabled: boolean;
            encrypt_names: boolean;
            passphrase?: string;
            passphrase_env?: string;
            passphrase_file?: string;
            master_salt_file: string;
            chunk_size: number;
          } = {
            enabled: enc.enabled,
            encrypt_names: enc.encrypt_names,
            master_salt_file: enc.master_salt_file,
            chunk_size: enc.chunk_size,
          };
          if (enc.enabled) {
            if (enc.passphrase_source === "passphrase") {
              block.passphrase = enc.passphrase;
            } else if (enc.passphrase_source === "passphrase_env") {
              block.passphrase_env = enc.passphrase_env;
            } else if (enc.passphrase_source === "passphrase_file") {
              block.passphrase_file = enc.passphrase_file;
            }
          }
          return block;
        })(),
      },
    };

    return yaml.dump(configForYaml, { indent: 2 });
  };

  const downloadConfig = () => {
    const yamlContent = generateYaml();
    const blob = new Blob([yamlContent], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "config.yaml";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const copyToClipboard = () => {
    const yamlContent = generateYaml();
    navigator.clipboard.writeText(yamlContent);
  };

  const regenerateJwtSecret = () => {
    updateConfig("tgdcfs.jwt.secret", generateRandomSecret());
  };

  const addUser = () => {
    const newUsers = [...config.tgdcfs.users, { username: "", password: "" }];
    updateConfig("tgdcfs.users", newUsers);
  };

  const removeUser = (index: number) => {
    const newUsers = config.tgdcfs.users.filter((_, i) => i !== index);
    updateConfig("tgdcfs.users", newUsers);
  };

  const updateUser = (
    index: number,
    field: "username" | "password",
    value: string
  ) => {
    const newUsers = [...config.tgdcfs.users];
    newUsers[index][field] = value;
    updateConfig("tgdcfs.users", newUsers);
  };

  return (
    <Container maxWidth="lg" sx={{ py: 4 }}>
      <Typography variant="h3" component="h1" gutterBottom align="center">
        TGDCFS Config Generator
      </Typography>

      <Typography
        variant="h6"
        color="text.secondary"
        align="center"
        sx={{ mb: 4 }}
      >
        Generate your TGDCFS configuration file with this interactive form
      </Typography>

      <Alert severity="warning" sx={{ mb: 3 }}>
        <AlertTitle>Important</AlertTitle>
        Keep your API credentials and bot tokens secure. Never share them
        publicly.
      </Alert>

      <Box
        sx={{
          display: "flex",
          gap: 3,
          flexDirection: { xs: "column", md: "row" },
        }}
      >
        <Box sx={{ flex: 1 }}>
          <Paper sx={{ p: 3 }}>
            <FormSection title="Stores" showDivider={false}>
              <Typography variant="body2" color="text.secondary">
                A store is one channel of one backend: a private Telegram
                channel or a Discord channel. Give each a short name; file
                systems below refer to stores by that name, and the name never
                reaches the stored metadata, so it can be changed later.
              </Typography>
              {config.stores.map((store, index) => (
                <StoreField
                  key={index}
                  store={store}
                  discordEnabled={config.discord.enabled}
                  onUpdate={(field, value) => updateStore(index, field, value)}
                  onDelete={
                    config.stores.length > 1
                      ? () => removeStore(index)
                      : undefined
                  }
                  nameErrors={getStoreNameErrors(index)}
                  channelErrors={getStoreChannelErrors(index)}
                />
              ))}
              <Button
                startIcon={<Add />}
                onClick={addStore}
                variant="outlined"
                size="small"
                sx={{ width: "fit-content" }}
              >
                Add Another Store
              </Button>
            </FormSection>

            <FormSection title="File Systems">
              <Typography variant="body2" color="text.secondary">
                Each file system is a top-level directory over WebDAV and SFTP.
                It has one primary store and any number of mirror stores;
                primary and mirror can be swapped later with a config change.
                Files that existed before a mirror was added are copied by the
                backfill task (Manager API:{" "}
                <code>POST /api/redundancy/backfill/&lt;file system&gt;</code>
                ).
              </Typography>
              {config.filesystems.map((filesystem, index) => (
                <FilesystemField
                  key={index}
                  filesystem={filesystem}
                  stores={config.stores}
                  onUpdate={(field, value) =>
                    updateFilesystem(index, field, value)
                  }
                  onUpdateGitHubRepo={(field, value) =>
                    updateFilesystemGitHubRepo(index, field, value)
                  }
                  onDelete={
                    config.filesystems.length > 1
                      ? () => removeFilesystem(index)
                      : undefined
                  }
                  nameErrors={getFilesystemNameErrors(index)}
                  primaryErrors={getFilesystemPrimaryErrors(index)}
                  mirrorErrors={getFilesystemMirrorErrors(index)}
                  needsSharedStore={filesystemNeedsSharedStore(index)}
                />
              ))}
              <Button
                startIcon={<Add />}
                onClick={addFilesystem}
                variant="outlined"
                size="small"
                sx={{ width: "fit-content" }}
              >
                Add Another File System
              </Button>
            </FormSection>

            <FormSection title="Telegram">
              {!telegramUsed && (
                <Alert severity="info">
                  No store uses Telegram; this block is left out of the
                  config.
                </Alert>
              )}
              <Box
                sx={{ display: "flex", alignItems: "center", gap: 2, mb: 2 }}
              >
                <Typography variant="h6">API Credentials</Typography>
                <Button
                  variant="outlined"
                  size="small"
                  component="a"
                  href="https://my.telegram.org/apps"
                  target="_blank"
                  rel="noopener noreferrer"
                  sx={{ textTransform: "none" }}
                >
                  Get API Keys
                </Button>
              </Box>
              <FieldRow justifyContent="space-between">
                <ConfigTextField
                  label="API ID"
                  value={config.telegram.api_id}
                  onChange={(e) =>
                    updateConfig("telegram.api_id", e.target.value)
                  }
                  style={{ flex: 1 }}
                  required={telegramUsed}
                />
                <ConfigTextField
                  label="API Hash"
                  value={config.telegram.api_hash}
                  onChange={(e) =>
                    updateConfig("telegram.api_hash", e.target.value)
                  }
                  style={{ flex: 1 }}
                  required={telegramUsed}
                />
                <FormControl size="small" sx={{ minWidth: 200 }}>
                  <InputLabel>Telegram Library</InputLabel>
                  <Select
                    value={config.telegram.lib}
                    label="Telegram Library"
                    onChange={(e) =>
                      updateConfig(
                        "telegram.lib",
                        e.target.value as "pyrogram" | "telethon"
                      )
                    }
                  >
                    <MenuItem value="pyrogram">Pyrogram</MenuItem>
                    <MenuItem value="telethon">Telethon</MenuItem>
                  </Select>
                </FormControl>
              </FieldRow>

              <FormControlLabel
                label="Use user account to upload files (No benefit unless you are a premium user)"
                control={
                  <Checkbox
                    checked={withUserAccountUpload}
                    onChange={(e) => {
                      setWithUserAccountUpload(e.target.checked);
                    }}
                  />
                }
              />
              <FormControlLabel
                label="Use user account to download files (No known benefit)"
                control={
                  <Checkbox
                    checked={withUserAccountDownload}
                    onChange={(e) => {
                      setWithUserAccountDownload(e.target.checked);
                    }}
                  />
                }
              />

              <Box>
                <Box
                  sx={{
                    display: "flex",
                    alignItems: "center",
                    gap: 2,
                    mt: 2,
                    mb: 2,
                  }}
                >
                  <Typography variant="h6">Bot Tokens</Typography>
                  <Button
                    variant="outlined"
                    size="small"
                    component="a"
                    href="https://t.me/botfather"
                    target="_blank"
                    rel="noopener noreferrer"
                    sx={{ textTransform: "none" }}
                  >
                    @BotFather
                  </Button>
                </Box>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                  Every bot must be admin in every Telegram store, mirrors
                  included. A primary channel with &quot;Restrict saving
                  content&quot; enabled cannot be forwarded from; the auto copy
                  mode then re-uploads.
                </Typography>
                {config.telegram.bot.tokens.map((token, index) => (
                  <BotTokenField
                    key={index}
                    index={index}
                    value={token}
                    onChange={(value) => updateBotToken(index, value)}
                    onDelete={
                      index > 0 ? () => removeBotToken(index) : undefined
                    }
                  />
                ))}
                <Button
                  startIcon={<Add />}
                  onClick={addBotToken}
                  variant="outlined"
                  size="small"
                  sx={{ mt: 1 }}
                >
                  Add Another Bot Token
                </Button>
              </Box>
            </FormSection>

            <FormSection title="Discord (Optional)">
              <Typography variant="body2" color="text.secondary">
                Discord channels can be stores too, as primaries or as mirrors.
                Create an application at{" "}
                <a
                  href="https://discord.com/developers/applications"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <u>discord.com/developers</u>
                </a>
                , add a bot, enable the Message Content intent and invite it
                with View Channel, Send Messages, Manage Messages, Read Message
                History, Attach Files and Pin Messages.
              </Typography>
              <FormControlLabel
                label="Enable the Discord backend"
                control={
                  <Checkbox
                    checked={config.discord.enabled}
                    onChange={(e) => updateDiscord("enabled", e.target.checked)}
                  />
                }
              />
              {config.discord.enabled && (
                <>
                  {config.discord.bot_tokens.map((token, index) => (
                    <BotTokenField
                      key={index}
                      index={index}
                      value={token}
                      onChange={(value) => updateDiscordToken(index, value)}
                      onDelete={
                        index > 0
                          ? () =>
                              updateDiscord(
                                "bot_tokens",
                                config.discord.bot_tokens.filter(
                                  (_, i) => i !== index
                                )
                              )
                          : undefined
                      }
                    />
                  ))}
                  <Button
                    startIcon={<Add />}
                    onClick={() =>
                      updateDiscord("bot_tokens", [
                        ...config.discord.bot_tokens,
                        "",
                      ])
                    }
                    variant="outlined"
                    size="small"
                    sx={{ width: "fit-content" }}
                  >
                    Add Another Bot Token
                  </Button>
                  <FieldRow>
                    <FormControl size="small" sx={{ minWidth: 260 }}>
                      <InputLabel>Attachment Limit</InputLabel>
                      <Select
                        value={config.discord.max_file_size_bytes}
                        label="Attachment Limit"
                        onChange={(e) =>
                          updateDiscord(
                            "max_file_size_bytes",
                            Number(e.target.value)
                          )
                        }
                      >
                        <MenuItem value={10000000}>
                          10 MB (safe everywhere)
                        </MenuItem>
                        <MenuItem value={20000000}>
                          20 MB (unboosted server, since Aug 2026)
                        </MenuItem>
                        <MenuItem value={50000000}>50 MB (boost level 2)</MenuItem>
                        <MenuItem value={100000000}>
                          100 MB (boost level 3)
                        </MenuItem>
                      </Select>
                    </FormControl>
                  </FieldRow>
                  <Typography variant="body2" color="text.secondary">
                    Files are cut into attachments of this size. A value above
                    what the server allows makes every upload fail, so pick the
                    tier of the server the channels are in.
                  </Typography>
                  <FormControlLabel
                    label="Delete Discord messages when a file is removed"
                    control={
                      <Checkbox
                        checked={config.discord.delete_messages_on_remove}
                        onChange={(e) =>
                          updateDiscord(
                            "delete_messages_on_remove",
                            e.target.checked
                          )
                        }
                      />
                    }
                  />
                </>
              )}
            </FormSection>

            <FormSection title="TGDCFS">
              <Box>
                <Typography variant="h6" sx={{ mb: 2 }}>
                  Users
                </Typography>
                {config.tgdcfs.users.map((user, index) => (
                  <UserField
                    key={index}
                    username={user.username}
                    password={user.password}
                    onUsernameChange={(username) =>
                      updateUser(index, "username", username)
                    }
                    onPasswordChange={(password) =>
                      updateUser(index, "password", password)
                    }
                    onDelete={index > 0 ? () => removeUser(index) : undefined}
                    canDelete={index > 0}
                  />
                ))}
                <Button
                  startIcon={<Add />}
                  onClick={addUser}
                  variant="outlined"
                  size="small"
                  sx={{ mt: 1, width: "fit-content" }}
                >
                  Add Another User
                </Button>
              </Box>
              <Typography variant="h6" sx={{ mt: 2, mb: 1 }}>
                JWT
              </Typography>
              <Box sx={{ display: "flex", gap: 1, mb: 2 }}>
                <ConfigTextField
                  label="JWT Secret"
                  value={config.tgdcfs.jwt.secret}
                  onChange={(e) =>
                    updateConfig("tgdcfs.jwt.secret", e.target.value)
                  }
                  sx={{ flex: 1 }}
                />
                <Button
                  variant="outlined"
                  size="small"
                  startIcon={<Refresh />}
                  onClick={regenerateJwtSecret}
                  sx={{ minWidth: "120px" }}
                >
                  Regenerate
                </Button>
              </Box>
              <Typography variant="h6" sx={{ mt: 2, mb: 1 }}>
                Server
              </Typography>
              <FieldRow>
                <ConfigTextField
                  label="Host"
                  value={config.tgdcfs.server.host}
                  onChange={(e) =>
                    updateConfig("tgdcfs.server.host", e.target.value)
                  }
                  width={200}
                />
                <ConfigTextField
                  label="Port"
                  type="number"
                  value={config.tgdcfs.server.port}
                  onChange={(e) =>
                    updateConfig("tgdcfs.server.port", parseInt(e.target.value))
                  }
                  width={120}
                />
              </FieldRow>
              <Typography variant="body2" color="text.secondary">
                WebDAV server will be at{" "}
                <code>
                  http://{config.tgdcfs.server.host}:{config.tgdcfs.server.port}
                  /webdav
                </code>
              </Typography>
              <Typography variant="body2" color="text.secondary">
                TGDCFS server will be at{" "}
                <code>
                  http://{config.tgdcfs.server.host}:{config.tgdcfs.server.port}
                </code>{" "}
                {"("}Used in the{" "}
                <a href="https://xyvran.github.io/tgdcfs/telegram-mini-app/">
                  <u>Telegram Mini App</u>
                </a>
                {")"}.
              </Typography>
            </FormSection>

            <FormSection title="SFTP (Optional)">
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Serves the same file tree over SFTP, next to WebDAV, using the
                same users and the same readonly flags. SSH cannot share the
                HTTP port, so it needs a port of its own.
              </Typography>
              <FormControlLabel
                label="Enable the SFTP interface"
                control={
                  <Checkbox
                    checked={config.tgdcfs.sftp.enabled}
                    onChange={(e) =>
                      updateConfig("tgdcfs.sftp", {
                        ...config.tgdcfs.sftp,
                        enabled: e.target.checked,
                      })
                    }
                  />
                }
              />
              {config.tgdcfs.sftp.enabled && (
                <>
                  <FieldRow>
                    <ConfigTextField
                      label="Host"
                      value={config.tgdcfs.sftp.host}
                      onChange={(e) =>
                        updateConfig("tgdcfs.sftp", {
                          ...config.tgdcfs.sftp,
                          host: e.target.value,
                        })
                      }
                      width={200}
                    />
                    <ConfigTextField
                      label="Port"
                      type="number"
                      value={config.tgdcfs.sftp.port}
                      onChange={(e) =>
                        updateConfig("tgdcfs.sftp", {
                          ...config.tgdcfs.sftp,
                          port: parseInt(e.target.value),
                        })
                      }
                      width={120}
                    />
                  </FieldRow>
                  <Typography variant="body2" color="text.secondary">
                    Connect with{" "}
                    <code>
                      sftp -P {config.tgdcfs.sftp.port} &lt;user&gt;@
                      {config.tgdcfs.sftp.host}
                    </code>
                  </Typography>
                  <FieldRow>
                    <ConfigTextField
                      label="Host Key File"
                      value={config.tgdcfs.sftp.host_key_file}
                      onChange={(e) =>
                        updateConfig("tgdcfs.sftp", {
                          ...config.tgdcfs.sftp,
                          host_key_file: e.target.value,
                        })
                      }
                      width={280}
                    />
                    <ConfigTextField
                      label="Upload Buffer (MB)"
                      type="number"
                      value={config.tgdcfs.sftp.upload_buffer_size_mb}
                      onChange={(e) =>
                        updateConfig("tgdcfs.sftp", {
                          ...config.tgdcfs.sftp,
                          upload_buffer_size_mb: parseInt(e.target.value),
                        })
                      }
                      width={180}
                    />
                  </FieldRow>
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{ mb: 1 }}
                  >
                    The host key is generated on first start and must be backed
                    up — a new one on every restart makes clients refuse to
                    connect. SFTP never announces an upload&apos;s size, so a
                    file is buffered in memory up to the size above and spills
                    to disk beyond it.
                  </Typography>
                  <ConfigTextField
                    label="Authorized Keys Directory (optional)"
                    value={config.tgdcfs.sftp.authorized_keys_dir}
                    onChange={(e) =>
                      updateConfig("tgdcfs.sftp", {
                        ...config.tgdcfs.sftp,
                        authorized_keys_dir: e.target.value,
                      })
                    }
                    width={360}
                  />
                  <Typography variant="body2" color="text.secondary">
                    Leave empty for password login only. Otherwise put one file
                    per user in that directory, named after the username, in
                    the usual <code>authorized_keys</code> format. The user
                    still has to be listed above so the readonly flag applies.
                  </Typography>
                </>
              )}
            </FormSection>

            <FormSection title="Encryption (Optional)">
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                At-rest encryption with AES-256-GCM. When enabled, every file
                is encrypted client-side before being uploaded; the Telegram
                channel only ever sees ciphertext plus a public per-file salt.
              </Typography>
              <EncryptionField
                config={config.tgdcfs.encryption}
                onUpdate={(field, value) =>
                  updateConfig("tgdcfs.encryption", {
                    ...config.tgdcfs.encryption,
                    [field]: value,
                  })
                }
              />
            </FormSection>

            <FormSection title="Transfer Performance (Optional)">
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                How file bytes are moved to and from Telegram. Every setting
                has a default that matches the behaviour you get without this
                block, so leave it off unless you want to tune something.
              </Typography>
              <FormControlLabel
                label="Tune transfer settings"
                control={
                  <Checkbox
                    checked={config.tgdcfs.transfer.enabled}
                    onChange={(e) =>
                      updateConfig("tgdcfs.transfer", {
                        ...config.tgdcfs.transfer,
                        enabled: e.target.checked,
                      })
                    }
                  />
                }
              />
              {config.tgdcfs.transfer.enabled && (
                <>
                  <Typography variant="subtitle2" sx={{ mt: 1 }}>
                    Downloads
                  </Typography>
                  <FieldRow>
                    <ConfigTextField
                      label="Piece Size (KB)"
                      type="number"
                      value={config.tgdcfs.transfer.download_piece_size_kb}
                      onChange={(e) =>
                        updateConfig("tgdcfs.transfer", {
                          ...config.tgdcfs.transfer,
                          download_piece_size_kb: parseInt(e.target.value),
                        })
                      }
                      width={170}
                    />
                    <ConfigTextField
                      label="Pieces In Flight"
                      type="number"
                      value={config.tgdcfs.transfer.download_pieces_in_flight}
                      onChange={(e) =>
                        updateConfig("tgdcfs.transfer", {
                          ...config.tgdcfs.transfer,
                          download_pieces_in_flight: parseInt(e.target.value),
                        })
                      }
                      width={170}
                    />
                    <ConfigTextField
                      label="Split Above (MB)"
                      type="number"
                      value={
                        config.tgdcfs.transfer.parallel_download_threshold_mb
                      }
                      onChange={(e) =>
                        updateConfig("tgdcfs.transfer", {
                          ...config.tgdcfs.transfer,
                          parallel_download_threshold_mb: parseInt(
                            e.target.value
                          ),
                        })
                      }
                      width={170}
                    />
                  </FieldRow>
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{ mb: 1 }}
                  >
                    A download is cut into pieces and several are fetched at
                    once. Bytes have to be handed out in order, so a piece
                    that arrives early waits its turn:{" "}
                    <strong>
                      peak buffering is{" "}
                      {(
                        (config.tgdcfs.transfer.download_piece_size_kb *
                          config.tgdcfs.transfer.download_pieces_in_flight) /
                        1024
                      ).toFixed(0)}{" "}
                      MiB per download
                    </strong>
                    , for every reader at the same time.
                  </Typography>

                  <Typography variant="subtitle2" sx={{ mt: 1 }}>
                    Connections
                  </Typography>
                  <ConfigTextField
                    label="Connections Per Bot"
                    type="number"
                    value={config.tgdcfs.transfer.connection_pool_size}
                    onChange={(e) =>
                      updateConfig("tgdcfs.transfer", {
                        ...config.tgdcfs.transfer,
                        connection_pool_size: parseInt(e.target.value),
                      })
                    }
                    width={200}
                  />
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{ mb: 1 }}
                  >
                    Pieces are handed to the bot tokens above in turn, so each
                    extra token is another connection a download can use. With
                    a single token, raise this instead: one connection sends
                    its requests one after another, so extra connections are
                    what let one bot overlap transfers.
                  </Typography>

                  <Typography variant="subtitle2" sx={{ mt: 1 }}>
                    Uploads
                  </Typography>
                  <FieldRow>
                    <ConfigTextField
                      label="Workers (Small Files)"
                      type="number"
                      value={config.tgdcfs.transfer.upload_workers_small}
                      onChange={(e) =>
                        updateConfig("tgdcfs.transfer", {
                          ...config.tgdcfs.transfer,
                          upload_workers_small: parseInt(e.target.value),
                        })
                      }
                      width={190}
                    />
                    <ConfigTextField
                      label="Workers (Large Files)"
                      type="number"
                      value={config.tgdcfs.transfer.upload_workers_big}
                      onChange={(e) =>
                        updateConfig("tgdcfs.transfer", {
                          ...config.tgdcfs.transfer,
                          upload_workers_big: parseInt(e.target.value),
                        })
                      }
                      width={190}
                    />
                    <FormControl size="small" sx={{ minWidth: 150 }}>
                      <InputLabel>Part Size (KB)</InputLabel>
                      <Select
                        label="Part Size (KB)"
                        value={config.tgdcfs.transfer.upload_part_size_kb}
                        onChange={(e) =>
                          updateConfig("tgdcfs.transfer", {
                            ...config.tgdcfs.transfer,
                            upload_part_size_kb: Number(e.target.value),
                          })
                        }
                      >
                        {[64, 128, 256, 512].map((size) => (
                          <MenuItem key={size} value={size}>
                            {size}
                          </MenuItem>
                        ))}
                      </Select>
                    </FormControl>
                  </FieldRow>
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{ mb: 1 }}
                  >
                    Telegram only accepts part sizes that divide 512 KB and
                    caps them there, which is why this is a fixed list. More
                    workers means more requests per second; if uploads start
                    logging flood waits, lower this and the connection count
                    before raising anything else.
                  </Typography>

                  <Typography variant="subtitle2" sx={{ mt: 1 }}>
                    Chunk Cache
                  </Typography>
                  <FieldRow>
                    <ConfigTextField
                      label="Cache Budget (MB)"
                      type="number"
                      value={config.tgdcfs.transfer.chunk_cache_mb}
                      onChange={(e) =>
                        updateConfig("tgdcfs.transfer", {
                          ...config.tgdcfs.transfer,
                          chunk_cache_mb: parseInt(e.target.value),
                        })
                      }
                      width={170}
                    />
                    <ConfigTextField
                      label="Block Size (KB)"
                      type="number"
                      value={config.tgdcfs.transfer.chunk_cache_block_kb}
                      onChange={(e) =>
                        updateConfig("tgdcfs.transfer", {
                          ...config.tgdcfs.transfer,
                          chunk_cache_block_kb: parseInt(e.target.value),
                        })
                      }
                      width={170}
                    />
                    <ConfigTextField
                      label="Read-Ahead Blocks"
                      type="number"
                      value={config.tgdcfs.transfer.chunk_cache_readahead}
                      onChange={(e) =>
                        updateConfig("tgdcfs.transfer", {
                          ...config.tgdcfs.transfer,
                          chunk_cache_readahead: parseInt(e.target.value),
                        })
                      }
                      width={170}
                    />
                  </FieldRow>
                  <Typography variant="body2" color="text.secondary">
                    Keeps downloaded blocks in memory, so readers that revisit
                    bytes stop fetching them twice: seeking in a video, or an
                    SFTP client walking a file in small reads. A budget of 0
                    disables it. A read of a few kilobytes pulls a whole
                    block, so a larger block serves more of the reads that
                    follow and wastes more on readers that jump around.
                  </Typography>
                </>
              )}
            </FormSection>
          </Paper>
        </Box>

        <Box sx={{ width: { xs: "100%", md: "400px" }, flexShrink: 0 }}>
          <Paper sx={{ p: 3, position: "sticky", top: 24 }}>
            <Typography variant="h6" gutterBottom>
              Generated Configuration
            </Typography>

            <Box sx={{ mb: 2 }}>
              <Button
                fullWidth
                variant="contained"
                startIcon={<Download />}
                onClick={downloadConfig}
                sx={{ mb: 1 }}
              >
                Download config.yaml
              </Button>
              <Button
                fullWidth
                variant="outlined"
                startIcon={<ContentCopy />}
                onClick={copyToClipboard}
              >
                Copy to Clipboard
              </Button>
            </Box>

            <Card variant="outlined">
              <CardContent sx={{ p: 0 }}>
                <SyntaxHighlighter
                  language="yaml"
                  style={vscDarkPlus}
                  customStyle={{
                    fontSize: "0.75rem",
                    margin: 0,
                  }}
                >
                  {generateYaml()}
                </SyntaxHighlighter>
              </CardContent>
            </Card>
          </Paper>
        </Box>
      </Box>
    </Container>
  );
}
