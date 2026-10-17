export interface FileTreeNode {
  kind: "directory" | "file";
  path: string;
  label: string;
  children: FileTreeNode[];
}

/** Keep Git's case-sensitive paths, sorting directories first and compacting single-directory chains. */
export function buildFileTree(paths: string[]): FileTreeNode[] {
  interface Branch {
    node: FileTreeNode;
    entries: Map<string, Branch>;
  }
  const root = new Map<string, Branch>();
  for (const path of paths) {
    const parts = path.split("/");
    let entries = root;
    parts.forEach((label, index) => {
      const kind = index === parts.length - 1 ? "file" : "directory";
      const key = `${kind}:${label}`;
      let branch = entries.get(key);
      if (!branch) {
        branch = { node: { kind, path: parts.slice(0, index + 1).join("/"), label, children: [] }, entries: new Map() };
        entries.set(key, branch);
      }
      entries = branch.entries;
    });
  }
  function compact(entries: Map<string, Branch>): FileTreeNode[] {
    return [...entries.values()].map(branch => {
      let node = { ...branch.node, children: compact(branch.entries) };
      while (node.kind === "directory" && node.children.length === 1 && node.children[0]!.kind === "directory") {
        const child = node.children[0]!;
        node = { ...child, label: `${node.label}/${child.label}` };
      }
      return node;
    }).sort((a, b) => a.kind === b.kind ? a.label.localeCompare(b.label, "en", { numeric: true }) : a.kind === "directory" ? -1 : 1);
  }
  return compact(root);
}
