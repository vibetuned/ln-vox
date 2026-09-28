// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// https://astro.build/config
export default defineConfig({
	integrations: [
		starlight({
			title: 'ln-vox',
			description:
				'Self-hosted novel-to-audiobook pipeline: Gemma 4 casts and directs, Dramabox / VibeVoice perform, ffmpeg mixes a chaptered .m4b with EPUB sync.',
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/vibetuned/ln-vox' }],
			customCss: ['./src/styles/custom.css'],
			lastUpdated: false,
			sidebar: [
				{ label: 'Start here', items: [{ autogenerate: { directory: 'start' } }] },
				{ label: 'Guides', items: [{ autogenerate: { directory: 'guides' } }] },
				{ label: 'Launcher (run_pipeline.sh)', items: [{ autogenerate: { directory: 'launcher' } }] },
				{ label: 'Pipeline stages', items: [{ autogenerate: { directory: 'stages' } }] },
				{
					label: 'Library API',
					items: [
						'library/overview',
						'library/config',
						'library/series',
						{ label: 'lnvox.llm', collapsed: true, items: [{ autogenerate: { directory: 'library/llm' } }] },
						{ label: 'lnvox.ingest', collapsed: true, items: [{ autogenerate: { directory: 'library/ingest' } }] },
						{ label: 'lnvox.voices', collapsed: true, items: [{ autogenerate: { directory: 'library/voices' } }] },
						{ label: 'lnvox.stages', collapsed: true, items: [{ autogenerate: { directory: 'library/stages' } }] },
						{ label: 'lnvox.tts', collapsed: true, items: [{ autogenerate: { directory: 'library/tts' } }] },
					],
				},
				{ label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
			],
		}),
	],
});
