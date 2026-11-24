import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// The documentation is hosted on GitHub Pages next to the web frontend,
// under the same repository's coordinates: https://<owner>.github.io/<repo>/docs/.
// Everything below follows GITHUB_REPOSITORY (set by the Pages workflow), so a
// fork publishes its own docs without editing this file.
const [owner, repo] = (process.env.GITHUB_REPOSITORY || 'Xyvran/tgdcfs').split('/');
const ownerLower = owner.toLowerCase();
const repoLower = repo.toLowerCase();
const pagesHost = `https://${ownerLower}.github.io`;
const pagesUrl = `${pagesHost}/${repoLower}`;
const dockerHubNamespace = (process.env.DOCKERHUB_NAMESPACE || ownerLower).toLowerCase();

const baseUrl = process.env.DOCS_BASE_URL || `/${repoLower}/docs/`;

const config: Config = {
  title: 'TGDCFS',
  tagline: 'Telegram & Discord as WebDAV and SFTP Storage Backends',
  favicon: 'img/favicon.ico',

  url: pagesHost,
  baseUrl: baseUrl,

  organizationName: owner,
  projectName: repo,

  onBrokenLinks: 'throw',

  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          routeBasePath: '/',
          editUrl: `https://github.com/${owner}/${repo}/tree/master/docusaurus/`,
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'TGDCFS Documentation',
      logo: {
        alt: 'TGDCFS Logo',
        src: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'usersSidebar',
          position: 'left',
          label: '👤 Users',
        },
        {
          type: 'docSidebar',
          sidebarId: 'techUsersSidebar',
          position: 'left',
          label: '🛠️ Technical Users',
        },
        {
          type: 'docSidebar',
          sidebarId: 'developersSidebar',
          position: 'left',
          label: '💻 Developers',
        },
        {
          href: `https://github.com/${owner}/${repo}`,
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Documentation',
          items: [
            {
              label: 'User Guide',
              to: '/users/',
            },
            {
              label: 'Technical User Guide',
              to: '/technical-users/',
            },
            {
              label: 'Developer Guide',
              to: '/developers/',
            },
          ],
        },
        {
          title: 'Project Links',
          items: [
            {
              label: 'GitHub Repository',
              href: `https://github.com/${owner}/${repo}`,
            },
            {
              label: 'Config Generator',
              href: `${pagesUrl}/config-generator/`,
            },
            {
              label: 'Getting Started Guide',
              href: `${pagesUrl}/getting-started/`,
            },
          ],
        },
        {
          title: 'Community & License',
          items: [
            {
              label: 'Apache 2.0 License',
              href: `https://github.com/${owner}/${repo}/blob/master/LICENSE`,
            },
            {
              label: 'Docker Hub',
              href: `https://hub.docker.com/r/${dockerHubNamespace}/tgdcfs`,
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} TGDCFS Project. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'yaml', 'json', 'python', 'docker', 'ini', 'markup'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
