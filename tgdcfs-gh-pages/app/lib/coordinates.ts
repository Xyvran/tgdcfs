// Where this copy of the project lives: the Docker images it publishes and
// the GitHub Pages site it is served from.
//
// next.config.ts derives these from GITHUB_REPOSITORY and
// DOCKERHUB_NAMESPACE at build time (the same inputs the publishing
// workflows use) and exposes them as NEXT_PUBLIC_* variables, so the
// config generator and the guides show the images of the fork they were
// built from. Nothing here is rewritten after the fact. The fallbacks only
// apply when the app is built without next.config.ts, e.g. by a tool that
// bypasses it.
//
// The variables must be read as literal `process.env.NEXT_PUBLIC_*`
// expressions: that is what Next.js replaces with the value at build time.

// Docker Hub image without a tag, e.g. "vulcanosoftware/tgdcfs".
export const DOCKERHUB_IMAGE =
  process.env.NEXT_PUBLIC_DOCKERHUB_IMAGE || "xyvran/tgdcfs";

// GHCR image without a tag, e.g. "ghcr.io/vulcanosoftware/tgdcfs/tgdcfs".
// The workflows publish it as ghcr.io/<owner>/<repo>/<image>.
export const GHCR_IMAGE =
  process.env.NEXT_PUBLIC_GHCR_IMAGE || "ghcr.io/xyvran/tgdcfs/tgdcfs";

// Root of this repository's GitHub Pages site, without a trailing slash.
export const PAGES_URL =
  process.env.NEXT_PUBLIC_PAGES_URL || "https://xyvran.github.io/tgdcfs";
