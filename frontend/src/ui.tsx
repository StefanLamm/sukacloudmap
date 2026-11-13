import React, {useEffect, useId, useRef} from 'react';
import {LoaderCircle, X} from 'lucide-react';

export function Spinner() {return <LoaderCircle className="spin" size={17}/>;}
export function ErrorMessage({text}: {text: string}) {return text ? <div className="error-message" role="alert">{text}</div> : null;}
export function Modal({title, description, onClose, children, busy = false, wide = false}: {title: string; description?: string; onClose: () => void; children: React.ReactNode; busy?: boolean; wide?: boolean}) {
  const dialog = useRef<HTMLDialogElement>(null); const id = useId();
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    element?.querySelector<HTMLInputElement | HTMLTextAreaElement>('input:not([type="checkbox"]), textarea')?.focus();
    return () => element?.close();
  }, []);
  return <dialog ref={dialog} aria-labelledby={id} className={'modal ' + (wide ? 'modal-wide' : '')} onCancel={e => {e.preventDefault(); if (!busy) onClose();}} onClick={e => {if (e.target === e.currentTarget && !busy) onClose();}}><div className="modal-inner"><div className="modal-heading"><div><h2 id={id}>{title}</h2>{description && <p className="muted">{description}</p>}</div><button className="icon-button" aria-label="关闭窗口" onClick={onClose} disabled={busy}><X size={20}/></button></div>{children}</div></dialog>;
}
