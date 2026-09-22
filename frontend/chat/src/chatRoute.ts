const NEW_CHAT_PATH = "/";
export const MODEL_SETTINGS_PATH = "/settings/models";
export const EXTENSION_PATHS = {
  plugins: "/extensions/plugins",
  mcp: "/extensions/mcp",
  skills: "/extensions/skills",
} as const;

export type ExtensionKind = keyof typeof EXTENSION_PATHS;

export function isModelSettingsPath(pathname: string): boolean {
  return /^\/settings\/models\/?$/.test(pathname);
}

export function extensionKindFromPath(pathname: string): ExtensionKind | null {
  for (const [kind, path] of Object.entries(EXTENSION_PATHS) as Array<[ExtensionKind, string]>) {
    if (pathname === path || pathname === `${path}/`) return kind;
  }
  return null;
}

export function isExtensionPath(pathname: string): boolean {
  return extensionKindFromPath(pathname) !== null;
}

export function sessionFromPath(pathname: string): string {
  const match = pathname.match(/^\/chat\/([^/]+)\/?$/);
  if (!match) return "";
  const id = decodeURIComponent(match[1]).trim();
  return id ? (id.startsWith("web:") ? id : `web:${id}`) : "";
}

export function pathForSession(sessionId: string): string {
  const id = sessionId.replace(/^web:/, "");
  return id ? `/chat/${encodeURIComponent(id)}` : NEW_CHAT_PATH;
}

export function routeKey(sessionId: string): string {
  return sessionId || "__new__";
}
