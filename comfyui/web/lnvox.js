// ln-vox ComfyUI extension: a read-only log panel on every ln-vox node.
//
// The Python side pushes the live tail of each stage's output as a
// `lnvox.log` event while it runs (runtime.push_log) and returns the final
// summary as `ui.text` when it finishes. Both land in the same panel, so a
// multi-hour s4 render shows its progress on the node itself.
//
// Uses the frontend's own text-preview widget (the one "Preview as Text"
// uses) when available, else falls back to a plain DOM textarea.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const LOG_WIDGET = "lnvox_log";

function isLnVox(nodeData) {
	return typeof nodeData?.category === "string" && nodeData.category.startsWith("ln-vox");
}

function previewApi() {
	return window.comfyAPI?.textPreviewWidgets;
}

function ensureLogWidget(node) {
	if (node.__lnvoxLog) return node.__lnvoxLog;
	const preview = previewApi();
	if (preview?.addTextPreviewWidgets) {
		preview.addTextPreviewWidgets(node);
		node.__lnvoxLog = { kind: "preview" };
		return node.__lnvoxLog;
	}
	const el = document.createElement("textarea");
	el.readOnly = true;
	el.spellcheck = false;
	el.placeholder = "ln-vox log";
	Object.assign(el.style, {
		width: "100%",
		minHeight: "120px",
		fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
		fontSize: "11px",
		opacity: "0.85",
		whiteSpace: "pre",
		resize: "vertical",
	});
	const widget = node.addDOMWidget(LOG_WIDGET, "lnvox-log", el, { serialize: false });
	node.__lnvoxLog = { kind: "dom", widget, el };
	return node.__lnvoxLog;
}

function setLog(node, text) {
	if (!node) return;
	const log = ensureLogWidget(node);
	const value = Array.isArray(text) ? text.join("\n") : String(text ?? "");
	if (log.kind === "preview") {
		previewApi()?.updateTextPreviewWidgets?.(node, { text: [value] });
	} else {
		log.el.value = value;
		log.el.scrollTop = log.el.scrollHeight;
	}
	node.setDirtyCanvas?.(true, false);
}

function findNode(id) {
	const graph = app.rootGraph ?? app.graph;
	if (!graph) return null;
	// Subgraph executions report "outer:inner" ids; the log belongs to the leaf.
	const leaf = String(id).split(":").pop();
	return graph.getNodeById?.(Number(leaf)) ?? graph.getNodeById?.(leaf) ?? null;
}

app.registerExtension({
	name: "lnvox.logPanel",

	async beforeRegisterNodeDef(nodeType, nodeData) {
		if (!isLnVox(nodeData)) return;

		const onNodeCreated = nodeType.prototype.onNodeCreated;
		nodeType.prototype.onNodeCreated = function () {
			const r = onNodeCreated?.apply(this, arguments);
			try {
				ensureLogWidget(this);
			} catch (e) {
				console.warn("[ln-vox] could not add log panel", e);
			}
			return r;
		};

		const onExecuted = nodeType.prototype.onExecuted;
		nodeType.prototype.onExecuted = function (message) {
			onExecuted?.apply(this, arguments);
			if (message?.text) setLog(this, message.text);
		};
	},

	async setup() {
		api.addEventListener("lnvox.log", ({ detail }) => {
			try {
				setLog(findNode(detail?.node), detail?.text ?? "");
			} catch (e) {
				console.warn("[ln-vox] log update failed", e);
			}
		});
	},
});
