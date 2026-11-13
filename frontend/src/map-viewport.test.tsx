import React from 'react';
import {fireEvent,render,screen} from '@testing-library/react';
import {expect,it,vi} from 'vitest';
import {MapViewport} from './map-viewport';

it('地图工具条能放大缩小，浏览器不支持原生全屏时仍可展开并按Escape退出',()=>{
 const zoomIn=vi.fn(),zoomOut=vi.fn(),resize=vi.fn();
 const {container}=render(<MapViewport label="门店地图" ready onZoomIn={zoomIn} onZoomOut={zoomOut} onResize={resize}><div>地图画布</div></MapViewport>);
 fireEvent.click(screen.getByRole('button',{name:'放大地图'}));fireEvent.click(screen.getByRole('button',{name:'缩小地图'}));
 expect(zoomIn).toHaveBeenCalledOnce();expect(zoomOut).toHaveBeenCalledOnce();
 fireEvent.click(screen.getByRole('button',{name:'全屏地图'}));expect((container.firstChild as HTMLElement).classList.contains('expanded')).toBe(true);
 expect(screen.getByRole('button',{name:'退出全屏'}).getAttribute('aria-pressed')).toBe('true');
 fireEvent.keyDown(window,{key:'Escape'});expect((container.firstChild as HTMLElement).classList.contains('expanded')).toBe(false);
});

it('地图未初始化时放大缩小不可用',()=>{
 render(<MapViewport label="仓库地图" ready={false} onZoomIn={()=>{}} onZoomOut={()=>{}} onResize={()=>{}}><div/></MapViewport>);
 expect(screen.getByRole('button',{name:'放大地图'}).hasAttribute('disabled')).toBe(true);
 expect(screen.getByRole('button',{name:'缩小地图'}).hasAttribute('disabled')).toBe(true);
});

it('模态框和transform祖先不会裁剪全屏回退，退出后恢复原布局且保留画布',()=>{
 const {container}=render(<div style={{transform:'translateX(0)'}}><dialog open><MapViewport label="位置地图" ready onZoomIn={()=>{}} onZoomOut={()=>{}} onResize={()=>{}}><div data-testid="canvas"/></MapViewport></dialog></div>);
 const canvas=screen.getByTestId('canvas');const dialog=container.querySelector('dialog')!;
 fireEvent.click(screen.getByRole('button',{name:'全屏地图'}));
 expect(dialog.classList.contains('map-fullscreen-ancestor')).toBe(true);
 expect(container.firstElementChild!.classList.contains('map-fullscreen-ancestor')).toBe(true);
 expect(screen.getByTestId('canvas')).toBe(canvas);
 fireEvent.click(screen.getByRole('button',{name:'退出全屏'}));
 expect(dialog.classList.contains('map-fullscreen-ancestor')).toBe(false);
 expect((container.firstElementChild as HTMLElement).style.transform).toBe('translateX(0)');
 expect(screen.getByTestId('canvas')).toBe(canvas);
});
