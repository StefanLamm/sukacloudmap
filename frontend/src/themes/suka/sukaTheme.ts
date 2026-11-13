import {defineTheme} from '@astryxdesign/core/theme';
import {neutralTheme} from '@astryxdesign/theme-neutral/built';

// Brand colors are centralized here; components only consume semantic tokens.
export const sukaTheme = defineTheme({
  name: 'suka',
  extends: neutralTheme,
  color: {accent: ['#b44c16', '#ef985f'], neutralStyle: 'warm', contrast: 'standard'},
  typography: {
    body: {family: 'system-ui', fallbacks: 'sans-serif'},
    heading: {family: 'system-ui', fallbacks: 'sans-serif'},
    scale: {base: 14, ratio: 1.2},
  },
  radius: {base: 4, multiplier: 1},
  motion: {tempo: 0.9, character: 'crisp'},
  localTokens: {'--suka-login-story-display':'flex'},
  adaptations: {rules:[{when:{width:{below:'md'}},value:{localTokens:{'--suka-login-story-display':'none'}}}]},
  components: {
    'section': {base: {borderRadius: 'var(--radius-container)'}},
    'table-header-cell': {base: {backgroundColor: 'var(--color-background-muted)', color: 'var(--color-text-secondary)'}},
    'side-nav-item': {selected: {color: 'var(--color-text-accent)', backgroundColor: 'var(--color-accent-muted)'}},
    'dialog-header': {base: {padding: 'var(--spacing-5)'}},
    'dialog': {base: {borderRadius: 'var(--radius-container)'}},
  },
});
