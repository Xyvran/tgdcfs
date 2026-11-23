import type { NextConfig } from "next";

// The coordinates of the repository this build belongs to, resolved like the
// publishing workflows and the Docusaurus site do: GITHUB_REPOSITORY is
// <owner>/<repo>, and the Docker Hub namespace is DOCKERHUB_NAMESPACE with a
// fallback to the owner. An unset or empty variable falls back to the
// upstream, so a plain local build keeps working.
const [owner, repo] = (process.env.GITHUB_REPOSITORY || "Xyvran/tgdcfs").split(
  "/",
);
// Docker Hub namespaces and GHCR paths are lowercase, whatever case the
// GitHub account and repository names have.
const dockerHubNamespace = (
  process.env.DOCKERHUB_NAMESPACE || owner
).toLowerCase();

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  basePath: "/tgdcfs",
  assetPrefix: "/tgdcfs",
  images: {
    loader: "custom",
    loaderFile: "image-loader.js",
  },
  // Read through app/lib/coordinates.ts. The images match what
  // docker-publish.yml pushes: <namespace>/tgdcfs on Docker Hub and
  // ghcr.io/<owner>/<repo>/tgdcfs on GHCR.
  env: {
    NEXT_PUBLIC_DOCKERHUB_IMAGE: `${dockerHubNamespace}/tgdcfs`,
    NEXT_PUBLIC_GHCR_IMAGE: `ghcr.io/${owner}/${repo}/tgdcfs`.toLowerCase(),
    NEXT_PUBLIC_PAGES_URL:
      `https://${owner}.github.io/${repo}`.toLowerCase(),
  },
};

export default nextConfig;
