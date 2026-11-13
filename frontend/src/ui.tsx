import React from 'react';
import {X} from 'lucide-react';
import {Button} from '@astryxdesign/core/Button';
import {Banner} from '@astryxdesign/core/Banner';
import {Dialog, DialogHeader} from '@astryxdesign/core/Dialog';
import {Spinner as AstryxSpinner} from '@astryxdesign/core/Spinner';
import {VStack} from '@astryxdesign/core/VStack';
import {HStack} from '@astryxdesign/core/HStack';
export {Field} from '@astryxdesign/core/Field';

export const controlStyle: React.CSSProperties = {
  font: 'inherit', color: 'var(--color-text-primary)', background: 'var(--color-background-surface)',
  border: 'var(--border-width) solid var(--color-border-emphasized)', borderRadius: 'var(--radius-element)',
  minHeight: 'var(--size-element-md)', padding: 'var(--spacing-2) var(--spacing-3)',
  width: '100%', minWidth: 0, maxWidth: '100%', boxSizing: 'border-box',
  outlineColor: 'var(--color-accent)', accentColor: 'var(--color-accent)',
};

// Native controls deliberately retain browser date/time, file, validation and event
// contracts. Astryx Field/Stack provide layout; all visual values use theme tokens.
export function Input({style, type = 'text', disabled, ...props}: React.InputHTMLAttributes<HTMLInputElement>) {
  const compact = type === 'checkbox' || type === 'radio';
  return <input {...props} type={type} disabled={disabled} style={compact ? {
    accentColor: 'var(--color-accent)', width: 'var(--spacing-4)', height: 'var(--spacing-4)',
    flexShrink: 0, cursor: disabled ? 'not-allowed' : 'pointer', ...style,
  } : {...controlStyle, opacity: disabled ? 0.55 : 1, ...style}}/>;
}
export function Select({style, disabled, ...props}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} disabled={disabled} style={{...controlStyle, opacity: disabled ? 0.55 : 1, ...style}}/>;
}
export function TextArea({style, ...props}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} style={{...controlStyle, minHeight: '8rem', resize: 'vertical', ...style}}/>;
}
function accessibleText(node: React.ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(accessibleText).join('');
  if (React.isValidElement<{children?: React.ReactNode}>(node)) return accessibleText(node.props.children);
  return '';
}
type ActionProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'destructive' | 'danger'; size?: 'sm' | 'md' | 'lg';
};
export function Action({children, className = '', disabled, type = 'submit', variant, size = 'sm', style, ...props}: ActionProps) {
  const appearance = variant === 'danger' ? 'destructive' : variant ??
    (/danger/.test(className) ? 'destructive' : /primary/.test(className) ? 'primary' : /text-button|icon-button/.test(className) ? 'ghost' : 'secondary');
  const label = props['aria-label'] || accessibleText(children).trim() || props.title || '操作';
  return <Button {...props} label={label} variant={appearance} size={size} type={type}
    isDisabled={disabled} {...{disabled}} className={className}
    style={{flexShrink: 0, ...style}}><HStack as="span" gap={2} vAlign="center" hAlign="center">{children}</HStack></Button>;
}
export function Spinner() {return <AstryxSpinner size="sm" aria-label="正在加载"/>;}
export function ErrorMessage({text}: {text: string}) {
  return text ? <Banner status="error" title={text} role="alert"/> : null;
}
export function Modal({title, description, onClose, children, busy = false, wide = false}: {
  title: string; description?: string; onClose: () => void; children: React.ReactNode; busy?: boolean; wide?: boolean;
}) {
  return <Dialog isOpen onOpenChange={open => {if (!open && !busy) onClose();}}
    purpose={busy ? 'required' : 'form'} width={wide ? '64rem' : '32rem'} maxHeight="90dvh" padding={0}>
    <DialogHeader title={title} subtitle={description} endContent={
      <Action type="button" variant="ghost" aria-label="关闭窗口" onClick={onClose} disabled={busy}><X size={20}/></Action>
    }/>
    <VStack gap={4} padding={5} style={{minWidth: 0}}>{children}</VStack>
  </Dialog>;
}
