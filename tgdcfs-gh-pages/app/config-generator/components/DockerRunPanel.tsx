import { ContentCopy, Download } from "@mui/icons-material";
import {
  Box,
  Button,
  Checkbox,
  FormControlLabel,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import { useEffect, useState } from "react";
import { ConfigTextField } from "./ConfigTextField";

type PathStyle = "unix" | "windows";
type Variant = "run" | "compose";

interface DockerRunPanelProps {
  // Image without a tag, e.g. "xyvran/tgdcfs".
  image: string;
  // Container name and the directory the image reads its config from.
  containerName: string;
  dataDir: string;
  // Directory name suggested on the host, e.g. ".tgdcfs".
  hostDirName: string;
  // Ports to publish, container port = host port.
  ports: number[];
  // Environment variables passed through from the shell (-e NAME).
  envVars: string[];
}

const defaultHostPath = (style: PathStyle, dirName: string): string =>
  style === "windows"
    ? `C:\\Users\\user\\${dirName}`
    : `/home/user/${dirName}`;

const saveTextFile = (name: string, text: string) => {
  const url = URL.createObjectURL(new Blob([text], { type: "text/yaml" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
};

// The docker run command, or the docker-compose.yml, for the config the
// form describes: one published port each, the data directory mounted
// where the image expects it and the passphrase variable passed through
// when encryption reads one.
export function DockerRunPanel({
  image,
  containerName,
  dataDir,
  hostDirName,
  ports,
  envVars,
}: DockerRunPanelProps) {
  const [variant, setVariant] = useState<Variant>("run");
  const [pathStyle, setPathStyle] = useState<PathStyle>("unix");
  const [hostPath, setHostPath] = useState(defaultHostPath("unix", hostDirName));
  const [detached, setDetached] = useState(false);
  const [copied, setCopied] = useState(false);

  // Pick the path style of the visitor's machine once, on the client.
  useEffect(() => {
    if (window.navigator.userAgent.includes("Windows")) {
      setPathStyle("windows");
      setHostPath(defaultHostPath("windows", hostDirName));
    }
  }, [hostDirName]);

  const changeStyle = (style: PathStyle | null) => {
    if (!style) return;
    setPathStyle(style);
    setHostPath(defaultHostPath(style, hostDirName));
  };

  const uniquePorts = Array.from(new Set(ports.filter((p) => p > 0)));

  const runCommand = [
    "docker run",
    detached ? "-d --restart unless-stopped" : "-it",
    "--pull=always",
    `--name ${containerName}`,
    ...uniquePorts.map((p) => `-p ${p}:${p}`),
    ...envVars.map((name) => `-e ${name}`),
    `-v "${hostPath}:${dataDir}"`,
    `${image}:latest`,
  ].join(" ");

  // Compose reads the passphrase from a .env file next to it, so the
  // value never sits in the compose file itself.
  const composeFile = [
    "services:",
    `  ${containerName}:`,
    `    image: ${image}:latest`,
    `    container_name: ${containerName}`,
    "    pull_policy: always",
    "    restart: unless-stopped",
    ...(uniquePorts.length > 0
      ? ["    ports:", ...uniquePorts.map((p) => `      - "${p}:${p}"`)]
      : []),
    ...(envVars.length > 0
      ? [
          "    environment:",
          ...envVars.map((name) => `      ${name}: \${${name}}`),
        ]
      : []),
    "    volumes:",
    `      - "${hostPath}:${dataDir}"`,
    "",
  ].join("\n");

  const text = variant === "run" ? runCommand : composeFile;

  const copy = () => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <Box>
      <Typography variant="h6" gutterBottom>
        Docker
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Put the downloaded config.yaml into the directory below; the
        sessions, the salt and the cache are written next to it.
      </Typography>
      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 2 }}>
        <ToggleButtonGroup
          value={variant}
          exclusive
          onChange={(_, value: Variant | null) => value && setVariant(value)}
          size="small"
        >
          <ToggleButton value="run">docker run</ToggleButton>
          <ToggleButton value="compose">docker compose</ToggleButton>
        </ToggleButtonGroup>
      </Box>
      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 2 }}>
        <Typography variant="body2" color="text.secondary">
          Path Style:
        </Typography>
        <ToggleButtonGroup
          value={pathStyle}
          exclusive
          onChange={(_, style) => changeStyle(style)}
          size="small"
        >
          <ToggleButton value="unix">Unix</ToggleButton>
          <ToggleButton value="windows">Windows</ToggleButton>
        </ToggleButtonGroup>
      </Box>
      <ConfigTextField
        label="Directory of config.yaml"
        value={hostPath}
        onChange={(e) => setHostPath(e.target.value)}
        width="100%"
        sx={{ mb: 1 }}
      />
      {variant === "run" && (
        <FormControlLabel
          label="Run in the background and restart with Docker"
          control={
            <Checkbox
              checked={detached}
              onChange={(e) => setDetached(e.target.checked)}
              size="small"
            />
          }
          sx={{ mb: 1 }}
        />
      )}
      <Box
        sx={{
          bgcolor: "#1e1e1e",
          color: "grey.100",
          p: 2,
          borderRadius: 1,
        }}
      >
        <Typography
          variant="body2"
          component="code"
          sx={{
            display: "block",
            whiteSpace: variant === "run" ? "normal" : "pre",
            wordBreak: "break-all",
            fontFamily: "monospace",
            fontSize: "0.75rem",
            overflowX: "auto",
          }}
        >
          {text}
        </Typography>
        <Box sx={{ display: "flex", gap: 1, mt: 1 }}>
          <Button
            size="small"
            startIcon={<ContentCopy />}
            onClick={copy}
            sx={{ color: "grey.400" }}
          >
            {copied ? "Copied" : variant === "run" ? "Copy Command" : "Copy"}
          </Button>
          {variant === "compose" && (
            <Button
              size="small"
              startIcon={<Download />}
              onClick={() => saveTextFile("docker-compose.yml", composeFile)}
              sx={{ color: "grey.400" }}
            >
              Download docker-compose.yml
            </Button>
          )}
        </Box>
      </Box>
      {variant === "compose" && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          Start it with <code>docker compose up -d</code> from the directory
          that holds the file.
        </Typography>
      )}
      {envVars.length > 0 && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          {variant === "run" ? (
            <>
              Export{" "}
              {envVars.map((name, i) => (
                <span key={name}>
                  {i > 0 ? " and " : ""}
                  <code>{name}</code>
                </span>
              ))}{" "}
              in the shell first; Docker passes the value through without it
              appearing in the command.
            </>
          ) : (
            <>
              Put{" "}
              {envVars.map((name, i) => (
                <span key={name}>
                  {i > 0 ? " and " : ""}
                  <code>{name}=...</code>
                </span>
              ))}{" "}
              into a <code>.env</code> file next to the compose file and keep
              that file out of version control.
            </>
          )}
        </Typography>
      )}
    </Box>
  );
}
