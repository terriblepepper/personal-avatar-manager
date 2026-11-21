import { mount, flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import ChatFilesManager from '../src/components/chat/ChatFilesManager.vue'
const request=vi.hoisted(()=>vi.fn())
vi.mock('../src/api/client',()=>({request,notifyError:vi.fn()}))
const item=(id:string)=>({id,filename:id+'.txt',content_type:'text/plain',size:1,referenced:true,description:'',download_url:'/api/conversations/a/files/'+id})
describe('conversation file selection',()=>{
  it('clears stale selection on conversation change and ignores late responses',async()=>{
    let resolveOld:(files:ReturnType<typeof item>[])=>void=()=>{}
    request.mockImplementation((path:string)=>path==='/conversations/a/files'?new Promise(resolve=>{resolveOld=resolve}):Promise.resolve([item('new')]))
    const wrapper=mount(ChatFilesManager,{props:{conversationId:'a',busy:false}})
    await wrapper.setProps({conversationId:'b'});await flushPromises();resolveOld([item('old')]);await flushPromises()
    expect(wrapper.emitted('selected')?.at(-1)).toEqual([['new']]);expect(wrapper.text()).not.toContain('old.txt')
    wrapper.unmount()
  })
  it('rejects uploads that exceed five referenced files',async()=>{
    request.mockResolvedValue(Array.from({length:5},(_,i)=>item(String(i))))
    const wrapper=mount(ChatFilesManager,{props:{conversationId:'a',busy:false}});await flushPromises()
    const input=wrapper.get<HTMLInputElement>('input[type=file]');Object.defineProperty(input.element,'files',{configurable:true,value:[new File(['a'],'new.txt')]})
    await input.trigger('change');await flushPromises()
    expect(request.mock.calls.filter(call=>call[1]==='POST')).toHaveLength(0);expect(wrapper.text()).toContain('最多同时引用五份')
    wrapper.unmount()
  })
})

it('blocks sending while uploading and releases the state on conversation switch',async()=>{
  request.mockReset()
  request.mockImplementation((path:string,method?:string)=>{
    if(method==='POST')return new Promise(()=>{})
    return Promise.resolve([])
  })
  const wrapper=mount(ChatFilesManager,{props:{conversationId:'a',busy:false}});await flushPromises()
  const input=wrapper.get<HTMLInputElement>('input[type=file]');Object.defineProperty(input.element,'files',{configurable:true,value:[new File(['x'],'file.txt')]})
  await input.trigger('change');await flushPromises();expect(wrapper.emitted('uploading')?.at(-1)).toEqual([true])
  await wrapper.setProps({conversationId:'b'});await flushPromises();expect(wrapper.emitted('uploading')?.at(-1)).toEqual([false]);expect(wrapper.emitted('selected')?.at(-1)).toEqual([[]])
  wrapper.unmount()
})

it('uploads multipart data and emits the returned persistent references',async()=>{
  request.mockReset()
  request.mockImplementation(async (_path:string,method?:string)=>method==='POST'?{items:[item('uploaded')]}:[])
  const wrapper=mount(ChatFilesManager,{props:{conversationId:'a',busy:false}});await flushPromises()
  const input=wrapper.get<HTMLInputElement>('input[type=file]');Object.defineProperty(input.element,'files',{configurable:true,value:[new File(['x'],'file.txt')]})
  await input.trigger('change');await flushPromises()
  const call=request.mock.calls.find(c=>c[1]==='POST');expect(call?.[0]).toBe('/conversations/a/files');expect(call?.[2]).toBeInstanceOf(FormData)
  expect(wrapper.emitted('selected')?.at(-1)).toEqual([['uploaded']]);expect(wrapper.emitted('uploading')?.at(-1)).toEqual([false]);wrapper.unmount()
})
