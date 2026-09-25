import { Delete } from "@mui/icons-material";
import {
  Box,
  Button,
  Checkbox,
  Chip,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  OutlinedInput,
  Select,
  Typography,
} from "@mui/material";
import {
  FilesystemConfig,
  MetadataType,
  MirrorMode,
  StoreConfig,
  SyncMode,
  needsReupload,
} from "../types";
import { ConfigTextField } from "./ConfigTextField";

interface FilesystemFieldProps {
  filesystem: FilesystemConfig;
  stores: StoreConfig[];
  onUpdate: <K extends keyof FilesystemConfig>(
    field: K,
    value: FilesystemConfig[K]
  ) => void;
  onUpdateGitHubRepo: (
    field: keyof FilesystemConfig["metadata"]["github_repo"],
    value: string
  ) => void;
  onDelete?: () => void;
  nameErrors?: string[];
  primaryErrors?: string[];
  mirrorErrors?: string[];
  // Set when a mirror is the primary of another file system.
  needsSharedStore?: boolean;
}

const storeLabel = (store: StoreConfig) =>
  `${store.name || "(unnamed)"} · ${store.backend}`;

export function FilesystemField({
  filesystem,
  stores,
  onUpdate,
  onUpdateGitHubRepo,
  onDelete,
  nameErrors = [],
  primaryErrors = [],
  mirrorErrors = [],
  needsSharedStore = false,
}: FilesystemFieldProps) {
  const namedStores = stores.filter((s) => s.name.trim() !== "");
  const mirrorCandidates = namedStores.filter(
    (s) => s.name !== filesystem.primary
  );
  const reuploadMirror = needsReupload(filesystem, namedStores);

  return (
    <Box sx={{ mb: 3, pb: 2, borderBottom: 1, borderColor: "divider" }}>
      <Box sx={{ display: "flex", alignItems: "flex-start", gap: 1, mb: 2 }}>
        <ConfigTextField
          label="File System Name"
          value={filesystem.name}
          onChange={(e) => onUpdate("name", e.target.value)}
          required
          error={nameErrors.length > 0}
          helperText={
            nameErrors.length > 0
              ? nameErrors.join("; ")
              : "Top-level directory over WebDAV and SFTP"
          }
          width={220}
        />
        <FormControl
          size="small"
          sx={{ minWidth: 220 }}
          error={primaryErrors.length > 0}
        >
          <InputLabel>Primary Store</InputLabel>
          <Select
            value={
              namedStores.some((s) => s.name === filesystem.primary)
                ? filesystem.primary
                : ""
            }
            label="Primary Store"
            onChange={(e) => onUpdate("primary", e.target.value)}
          >
            {namedStores.map((store) => (
              <MenuItem key={store.name} value={store.name}>
                {storeLabel(store)}
              </MenuItem>
            ))}
          </Select>
          {primaryErrors.length > 0 && (
            <Typography variant="caption" color="error" sx={{ pl: 2 }}>
              {primaryErrors.join("; ")}
            </Typography>
          )}
        </FormControl>
        <FormControl size="small" sx={{ minWidth: 200 }}>
          <InputLabel>Metadata Type</InputLabel>
          <Select
            value={filesystem.metadata.type}
            label="Metadata Type"
            onChange={(e) =>
              onUpdate("metadata", {
                ...filesystem.metadata,
                type: e.target.value as MetadataType,
              })
            }
          >
            <MenuItem value="pinned_message">Pinned Message</MenuItem>
            <MenuItem value="github_repo">GitHub Repository</MenuItem>
          </Select>
        </FormControl>
        {onDelete && (
          <IconButton
            color="error"
            onClick={onDelete}
            sx={{ mt: 0.5 }}
            size="small"
          >
            <Delete />
          </IconButton>
        )}
      </Box>

      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, pl: 2 }}>
        {filesystem.metadata.type === "pinned_message"
          ? "The directory tree is kept as a JSON document pinned in the primary store and copied to every mirror. Never delete the pinned file or pin anything else. On a Discord primary the tree has to fit one attachment; use GitHub metadata for anything but small trees."
          : "The directory tree is kept in a GitHub repository, so it survives even the loss of every store. Recommended whenever mirrors are configured."}
      </Typography>

      {filesystem.metadata.type === "github_repo" && (
        <>
          <Box
            sx={{ display: "flex", alignItems: "flex-start", gap: 1, mb: 1 }}
          >
            <ConfigTextField
              label="Repository"
              value={filesystem.metadata.github_repo.repo}
              onChange={(e) => onUpdateGitHubRepo("repo", e.target.value)}
              helperText="Format: username/repository-name"
              required
              style={{ flex: 1 }}
            />
            <ConfigTextField
              label="Commit/Branch"
              value={filesystem.metadata.github_repo.commit}
              onChange={(e) => onUpdateGitHubRepo("commit", e.target.value)}
              width={200}
            />
          </Box>
          <Box
            sx={{ display: "flex", alignItems: "flex-start", gap: 1, mb: 2 }}
          >
            <ConfigTextField
              label="Access Token"
              value={filesystem.metadata.github_repo.access_token}
              onChange={(e) =>
                onUpdateGitHubRepo("access_token", e.target.value)
              }
              type="password"
              required
              style={{ flex: 1 }}
            />
            <Button
              variant="outlined"
              component="a"
              size="large"
              href="https://github.com/settings/personal-access-tokens/new"
              target="_blank"
              rel="noopener noreferrer"
              sx={{ textTransform: "none" }}
            >
              Get Token
            </Button>
          </Box>
        </>
      )}

      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        Mirrors
      </Typography>
      <Box sx={{ display: "flex", alignItems: "flex-start", gap: 1, mb: 1 }}>
        <FormControl
          size="small"
          sx={{ minWidth: 300, flex: 1 }}
          error={mirrorErrors.length > 0}
        >
          <InputLabel>Mirror Stores</InputLabel>
          <Select
            multiple
            value={filesystem.mirrors.filter((name) =>
              mirrorCandidates.some((s) => s.name === name)
            )}
            label="Mirror Stores"
            input={<OutlinedInput label="Mirror Stores" />}
            onChange={(e) =>
              onUpdate(
                "mirrors",
                (typeof e.target.value === "string"
                  ? e.target.value.split(",")
                  : e.target.value) as string[]
              )
            }
            renderValue={(selected) => (
              <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
                {(selected as string[]).map((value) => (
                  <Chip key={value} label={value} size="small" />
                ))}
              </Box>
            )}
          >
            {mirrorCandidates.map((store) => (
              <MenuItem key={store.name} value={store.name}>
                {storeLabel(store)}
              </MenuItem>
            ))}
          </Select>
          {mirrorErrors.length > 0 && (
            <Typography variant="caption" color="error" sx={{ pl: 2 }}>
              {mirrorErrors.join("; ")}
            </Typography>
          )}
        </FormControl>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1, pl: 2 }}>
        Every file lands in the primary and is copied to each mirror. A
        mirror in another backend, or with smaller messages, receives a
        re-uploaded copy. Swapping primary and a mirror later is a config
        change.
      </Typography>

      {filesystem.mirrors.length > 0 && (
        <>
          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1, mb: 1 }}>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Copy Mode</InputLabel>
              <Select
                value={filesystem.mode}
                label="Copy Mode"
                onChange={(e) =>
                  onUpdate("mode", e.target.value as MirrorMode)
                }
              >
                <MenuItem value="auto">Auto (recommended)</MenuItem>
                <MenuItem value="forward">Forward only</MenuItem>
                <MenuItem value="reupload">Re-upload only</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Sync</InputLabel>
              <Select
                value={filesystem.sync}
                label="Sync"
                onChange={(e) => onUpdate("sync", e.target.value as SyncMode)}
              >
                <MenuItem value="inline">Inline (during the upload)</MenuItem>
                <MenuItem value="background">Background queue</MenuItem>
              </Select>
            </FormControl>
          </Box>
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ mb: 1, pl: 2 }}
          >
            &quot;Auto&quot; copies server-side between Telegram channels
            (no bandwidth) and re-uploads where that is impossible.
            &quot;Inline&quot; makes an upload wait for its copies;
            &quot;Background&quot; queues the file and a worker copies it
            afterwards. Background is preselected as soon as a mirror
            re-uploads, since a client would otherwise time out waiting
            for a large upload to pass through the mirror a second time.
            {reuploadMirror && filesystem.sync === "inline" && (
              <>
                {" "}
                <strong>
                  One of the mirrors re-uploads every byte; switch Sync to
                  Background unless uploads may wait for it.
                </strong>
              </>
            )}
          </Typography>
          <FormControlLabel
            label="Strict: fail an upload when a mirror copy fails (needs inline sync)"
            control={
              <Checkbox
                checked={filesystem.strict}
                disabled={filesystem.sync !== "inline"}
                onChange={(e) => onUpdate("strict", e.target.checked)}
              />
            }
          />
          {needsSharedStore && (
            <FormControlLabel
              label="Allow a mirror that is the primary of another file system"
              control={
                <Checkbox
                  checked={filesystem.allow_shared_store}
                  onChange={(e) =>
                    onUpdate("allow_shared_store", e.target.checked)
                  }
                />
              }
            />
          )}
        </>
      )}
    </Box>
  );
}
