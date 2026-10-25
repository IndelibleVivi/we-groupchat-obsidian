# Architecture diagram sources

The functional maps explain the supported product path: selected conversations
become continuing Items, readable pages, search, briefings, follow-up and reading
controls. Source-first plans preserve legacy knowledge as separate readable
history; older legacy-semantic plans retain their frozen import contract. The independent resource lane keeps
link identity, attachment-byte consent, backup and external consumer delivery
visible. Source capability does not establish a completed private migration.

- [Chinese functional map](semantic-functional.zh-CN.excalidraw)
- [English functional map](semantic-functional.en.excalidraw)
- [Chinese module and authority map](semantic-core.zh-CN.excalidraw)
- [English module and authority map](semantic-core.en.excalidraw)
- [Complete technical Mermaid](semantic-implementation.zh-CN.mmd)
- [Implementation walkthrough, with expandable full reference](semantic-implementation.md)
- [Portable technical SVG](../assets/architecture/semantic-implementation.zh-CN.svg)

The `.excalidraw` files are editable source. Functional-map exports are generated
from those exact elements with the dependency-free exporter:

```bash
node docs/architecture/export-functional-map.mjs
```

Its output is the paired `docs/assets/architecture/semantic-functional.*.svg`.
The exporter reads live text, regions and bound edges; it does not infer new
features. Chinese and English maps keep the same semantic element IDs and edge
bindings, with deliberate language-specific wrapping and canvas height.
Render and visually inspect both exports after editing. At a 900 px display
width, body text remains readable; open the standalone SVG for a larger view.

The `.mmd` file is the editable source for the complete technical flowchart.
Its companion Markdown starts with a compact inline flow and a plain-language
walkthrough. The complete reference and source tables are collapsed below it;
no separate diagram viewer is required. Renderer output is derived from the
same complete Mermaid source.
With a Mermaid CLI renderer already installed, regenerate the portable SVG:

```bash
mmdc -i docs/architecture/semantic-implementation.zh-CN.mmd \
  -o docs/assets/architecture/semantic-implementation.zh-CN.svg
```

The complete technical graph is wide. Inspect it at readable zoom and use the
source anchor table to follow a specific lane; it is not a README thumbnail.

The semantic-core maps retain the implementation and state-owner view. The
`we-groupchat-obsidian-architecture.*` maps document the pre-migration workflow
and retained source/resource foundation; they are not the sole functional
description of an activated semantic generation.
