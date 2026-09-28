"""ln-vox nodes for ComfyUI — drive the novel-to-audiobook pipeline as a graph.

Install by symlinking this folder into ComfyUI's custom_nodes/:

    ln -s /path/to/ln-vox/comfyui /path/to/ComfyUI/custom_nodes/comfyui-lnvox

See README.md here, or the "ComfyUI" guide in the docs site (docs/).
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
