import React,{useState} from 'react';
import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {expect,it,vi} from 'vitest';
import {RegionSurchargeEditor,RegionSurcharge} from './region-picker';
vi.mock('./api',()=>({api:vi.fn(async()=>({items:[{code:'340000',name:'安徽省',children:[{code:'340100',name:'合肥市',children:[{code:'340104',name:'蜀山区'},{code:'340111',name:'包河区'}]},{code:'340400',name:'淮南市',children:[{code:'340403',name:'田家庵区'}]}]}]}))}));
function Editor(){const [value,setValue]=useState<RegionSurcharge>({amount:'80',targets:[]});return <><RegionSurchargeEditor value={value} onChange={setValue}/><output data-testid="value">{JSON.stringify(value)}</output></>;}
const value=()=>JSON.parse(screen.getByTestId('value').textContent!);
async function province(){await waitFor(()=>expect(screen.getByRole('option',{name:'安徽省'})).toBeTruthy());fireEvent.change(screen.getByLabelText('特殊地区省份'),{target:{value:'340000'}});}
it('收费方式可选按区县，支持一次勾选多个区县并保持旧规则默认一次性',async()=>{
 render(<Editor/>);expect((screen.getByLabelText('附加费收取方式') as HTMLSelectElement).value).toBe('ONCE');
 fireEvent.change(screen.getByLabelText('附加费收取方式'),{target:{value:'PER_REGION'}});expect(screen.getByLabelText('每个区县附加费（元）')).toBeTruthy();
 await province();fireEvent.change(screen.getByLabelText('特殊地区城市'),{target:{value:'340100'}});
 fireEvent.click(screen.getByRole('checkbox',{name:'附加费区县 蜀山区'}));fireEvent.click(screen.getByRole('checkbox',{name:'附加费区县 包河区'}));fireEvent.click(screen.getByRole('button',{name:'添加所选城市 / 区县'}));
 expect(value().billing_mode).toBe('PER_REGION');expect(value().targets.map((r:any)=>r.district_code)).toEqual(['340104','340111']);
 expect(screen.getByText(/同区县多店只收一次/)).toBeTruthy();
});
it('可同时选择多个城市，区县全选不会造成重复目标',async()=>{
 render(<Editor/>);await province();
 fireEvent.click(screen.getByRole('checkbox',{name:'附加费城市 合肥市'}));fireEvent.click(screen.getByRole('checkbox',{name:'附加费城市 淮南市'}));fireEvent.click(screen.getByRole('button',{name:'添加所选城市 / 区县'}));
 expect(value().targets.map((r:any)=>r.city_code)).toEqual(['340100','340400']);
 fireEvent.change(screen.getByLabelText('特殊地区城市'),{target:{value:'340100'}});fireEvent.click(screen.getByRole('button',{name:'全选所有区县'}));fireEvent.click(screen.getByRole('button',{name:'添加所选城市 / 区县'}));
 expect(value().targets).toHaveLength(4);
 fireEvent.click(screen.getByRole('button',{name:'全选所有区县'}));fireEvent.click(screen.getByRole('button',{name:'添加所选城市 / 区县'}));expect(value().targets).toHaveLength(4);
});
it('同区县一次与每家门店收费可切换，已添加地区及金额保留',async()=>{
 render(<Editor/>);await province();fireEvent.change(screen.getByLabelText('特殊地区城市'),{target:{value:'340100'}});
 fireEvent.click(screen.getByRole('checkbox',{name:'附加费区县 蜀山区'}));fireEvent.click(screen.getByRole('checkbox',{name:'附加费区县 包河区'}));fireEvent.click(screen.getByRole('button',{name:'添加所选城市 / 区县'}));
 fireEvent.change(screen.getByLabelText('附加费收取方式'),{target:{value:'PER_STORE'}});
 expect(screen.getByLabelText('每家门店附加费（元）')).toBeTruthy();expect(value().billing_mode).toBe('PER_STORE');expect(value().amount).toBe('80');expect(value().targets).toHaveLength(2);
 expect(screen.getByText(/同一区县的多家门店分别计费/)).toBeTruthy();
 fireEvent.change(screen.getByLabelText('附加费收取方式'),{target:{value:'PER_REGION'}});
 expect(screen.getByLabelText('每个区县附加费（元）')).toBeTruthy();expect(value().targets).toHaveLength(2);
});
