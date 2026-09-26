import { describe, expect, it } from 'vitest';
import { boundedRect, changeMask, fromPoints, removeMask } from './geometry';
import type { EditState, Mask } from './types';
describe('image-space mask editing', () => {
  it('keeps dragged and resized rectangles inside the full-resolution image', () => {
    expect(boundedRect({x:-12,y:70,width:70,height:50},100,100)).toEqual({x:0,y:50,width:70,height:50});
    expect(boundedRect({x:90,y:70,width:150,height:150},100,100)).toEqual({x:0,y:0,width:100,height:100});
  });
  it('supports drawing in any direction', () => {
    expect(fromPoints({x:100,y:90},{x:10,y:20})).toEqual({x:10,y:20,width:90,height:70});
  });
  it('excludes automatic masks without changing rules or manual masks', () => {
    const edits: EditState = {manual:[{id:'m',box:{x:1,y:1,width:3,height:3}}],excluded:[],overrides:{}};
    const mask: Mask = {id:'a',source:'auto',box:{x:10,y:10,width:5,height:5},reasons:[],fallback:false};
    const result=removeMask(edits,mask);
    expect(result.excluded).toEqual(['a']);
    expect(result.manual).toEqual(edits.manual);
    expect(edits.excluded).toEqual([]);
    expect(removeMask(result,mask).excluded).toEqual(['a']);
  });
  it('moves an automatic mask through an override, preserving its identity', () => {
    const edits: EditState = {manual:[],excluded:[],overrides:{}};
    const mask: Mask={id:'a',source:'auto',box:{x:1,y:1,width:4,height:4},reasons:[],fallback:false};
    const box={x:10,y:10,width:30,height:30};
    expect(changeMask(edits,mask,box).overrides.a).toEqual(box);
    expect(edits.overrides).toEqual({});
  });
});
