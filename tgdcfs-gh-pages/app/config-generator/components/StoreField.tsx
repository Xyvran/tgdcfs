import { Delete } from "@mui/icons-material";
import {
  Box,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Typography,
} from "@mui/material";
import { Backend, StoreConfig } from "../types";
import { ConfigTextField } from "./ConfigTextField";

interface StoreFieldProps {
  store: StoreConfig;
  discordEnabled: boolean;
  onUpdate: (field: keyof StoreConfig, value: string) => void;
  onDelete?: () => void;
  nameErrors?: string[];
  channelErrors?: string[];
}

export function StoreField({
  store,
  discordEnabled,
  onUpdate,
  onDelete,
  nameErrors = [],
  channelErrors = [],
}: StoreFieldProps) {
  return (
    <Box sx={{ mb: 2 }}>
      <Box sx={{ display: "flex", alignItems: "flex-start", gap: 1, mb: 1 }}>
        <ConfigTextField
          label="Store Name"
          value={store.name}
          onChange={(e) => onUpdate("name", e.target.value)}
          required
          error={nameErrors.length > 0}
          helperText={
            nameErrors.length > 0
              ? nameErrors.join("; ")
              : "Used in this file only; file systems refer to it"
          }
          width={200}
        />
        <FormControl size="small" sx={{ minWidth: 150 }}>
          <InputLabel>Backend</InputLabel>
          <Select
            value={store.backend}
            label="Backend"
            onChange={(e) => onUpdate("backend", e.target.value as Backend)}
          >
            <MenuItem value="telegram">Telegram</MenuItem>
            <MenuItem value="discord" disabled={!discordEnabled}>
              Discord{discordEnabled ? "" : " (enable the backend first)"}
            </MenuItem>
          </Select>
        </FormControl>
        <ConfigTextField
          label={store.backend === "discord" ? "Channel ID (numeric)" : "Channel ID"}
          value={store.channel}
          onChange={(e) => onUpdate("channel", e.target.value)}
          required
          error={channelErrors.length > 0}
          helperText={channelErrors.join("; ")}
          style={{ flex: 1 }}
        />
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
      <Typography variant="caption" color="text.secondary" sx={{ pl: 2 }}>
        {store.backend === "telegram"
          ? "A private Telegram channel your bot(s) are admin in. The id as shown by @userinfobot or the channel link."
          : "A Discord channel the bot can read, write, pin and delete in. Copy the id with developer mode enabled."}
      </Typography>
    </Box>
  );
}
