import type { ReactNode } from "react";

/** The official favicons of the services and editors reviewr works with, from
 *  public/brands. Used as small marks next to links and buttons. */
const BRAND_SRC = {
  github: "/brands/github.svg",
  openai: "/brands/openai.svg",
  atlassian: "/brands/atlassian.png",
  vscode: "/brands/vscode.png",
  cursor: "/brands/cursor.png",
  pycharm: "/brands/pycharm.svg",
} as const;
export type Brand = keyof typeof BRAND_SRC;

export function BrandIcon({ name, size = 14 }: { name: Brand; size?: number }) {
  return <img className={`brand-icon brand-${name}`} src={BRAND_SRC[name]} alt="" width={size} height={size} aria-hidden="true" />;
}

/** The selected editor's icon, or a generic one for editors reviewr has no icon for. */
export function EditorIcon({ editor, size = 14 }: { editor: string; size?: number }) {
  if (editor in BRAND_SRC) return <BrandIcon name={editor as Brand} size={size} />;
  return (
    <svg className="brand-icon" viewBox="0 0 16 16" width={size} height={size} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 4L1.5 8 5 12M11 4l3.5 4-3.5 4M9.5 2.5l-3 11" />
    </svg>
  );
}

/** "Open in editor", with the selected editor's icon; its name is in the tooltip. */
export function OpenInEditor({ editor, onClick, className = "link small" }: { editor: string; onClick: () => void; className?: string }) {
  return (
    <button className={`${className} with-icon`} onClick={onClick} title={`Open in ${editor}`}>
      <EditorIcon editor={editor} /> Open in editor
    </button>
  );
}

/** A link to GitHub with its mark. */
export function GitHubLink({ href, children, className }: { href: string | undefined; children: ReactNode; className?: string }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className={`with-icon ${className ?? ""}`}>
      <BrandIcon name="github" /> {children}
    </a>
  );
}
