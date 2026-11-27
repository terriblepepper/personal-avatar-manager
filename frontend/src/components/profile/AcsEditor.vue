<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AcsDeployment from './AcsDeployment.vue'
import MarkdownContent from '../MarkdownContent.vue'
import MimeEditor from './MimeEditor.vue'
import AcsListField from './AcsListField.vue'
import { request } from '../../api/client'
interface Skill {id:string;name:string;description:string;version:string;tags:string[];examples:string[];input_modes:string[];output_modes:string[]}
interface Provider {country_code:string;organization:string;department:string;name:string;url:string;license:string;email:string;domain_registrations:{domain:string;registration_number:string;registration_type:string}[]}
export interface AcsInput {template_owner?:string;name:string;description:string;version:string;icon_url:string;documentation_url:string;location:string;provider:Provider;default_input_modes:string[];default_output_modes:string[];skills:Skill[]}
const props=defineProps<{modelValue:AcsInput|null; document:unknown; modified:string; locked:boolean; online:boolean; editing:boolean; inherited:boolean; registered?:boolean; direct:boolean; profile:Record<string,unknown>}>()
const emit=defineEmits<{ 'update:modelValue':[value:AcsInput|null]; edit:[value:boolean] }>()
const editing=computed(()=>props.editing || !props.online), preview=ref<unknown>(props.document), errors=ref(''), valid=ref(props.inherited||props.online&&!props.editing), downloadUrl=ref('')
watch(preview,value=>{if(downloadUrl.value)URL.revokeObjectURL(downloadUrl.value);downloadUrl.value=value?URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'})):''},{immediate:true})
const tab = ref<'basic' | 'skills' | 'provider'>('basic')
const inheritedSkills = computed(() => {
  const doc = props.document
  return doc && typeof doc === 'object' && 'skills' in doc && Array.isArray(doc.skills)
    ? doc.skills.filter((item): item is Pick<Skill,'id'|'name'|'description'> => !!item && typeof item.name==='string') : []
})
const state = computed(() => props.inherited ? (props.registered ? '正式派生身份' : '继承个人分身模板')
  : !editing.value ? '已上线' : props.online ? '修改草稿' : '候选配置')
const basicFields = { name:'名称 *',version:'版本 *',description:'能力描述 *',icon_url:'图标地址',documentation_url:'文档地址',location:'地点' } as const
const providerFields = { country_code:'国家或地区代码',organization:'学校 / 组织',department:'部门',name:'提供者名称',url:'网址',license:'许可证',email:'电子邮件' } as const
const required = (skill: Skill) => skill.id==='personal-profile.introduction.qa'
watch(()=>props.document,value=>{ preview.value=value; if(!editing.value || props.inherited)valid.value=!!value })
function addSkill(){props.modelValue?.skills.push({id:'skill.'+Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join(''),name:'',description:'',version:'1.0.0',tags:[],examples:[],input_modes:[...props.modelValue.default_input_modes],output_modes:[...props.modelValue.default_output_modes]})}
let sequence=0, timer:ReturnType<typeof setTimeout>|undefined
async function validate(reset = false) {
  if (!props.modelValue || !editing.value || props.inherited) return
  clearTimeout(timer)
  const ticket = ++sequence
  errors.value = '正在校验…'
  try {
    const p = props.profile
    const result = await request<{
      document: { name: string; description: string }
      validation: { valid: boolean; errors: string[] }
      generated_description?: string
    }>('/me/acs/preview', 'POST', {
      acs_input: props.modelValue,
      last_modified_at: props.modified,
      profile_name: p.name || '',
      profile_avatar_name: p.avatar_name || '',
      profile_bio: p.bio || '',
      profile_organization: p.organization || '',
      name_visibility: 'public',
      bio_visibility: visibility('bio'),
      organization_visibility: visibility('organization')
    })
    if (ticket !== sequence) return
    preview.value = result.document
    valid.value = result.validation.valid
    if (reset && props.modelValue)
      props.modelValue.description = result.generated_description ?? result.document.description
    errors.value = result.validation.valid ? '校验通过' : result.validation.errors.join('；')
  } catch (e) {
    if (ticket === sequence) {
      valid.value = false
      errors.value = e instanceof Error ? e.message : '校验失败'
    }
  }
}
const skillPrefix=computed(()=>(String(props.profile.name || '').trim() || '该用户')+'能够')
function suffix(description:string){return description.startsWith(skillPrefix.value)?description.slice(skillPrefix.value.length):description}
function updateSkill(skill:Skill,event:Event){const value=(event.target as HTMLTextAreaElement).value;skill.description=value?skillPrefix.value+value:''}
watch(()=>[props.modelValue,props.profile,editing.value],()=>{sequence++;clearTimeout(timer);if(editing.value&&!props.inherited){valid.value=false;timer=setTimeout(validate,400)}else{valid.value=!!props.document;errors.value=''}},{deep:true})
onMounted(()=>{if(editing.value&&!props.inherited)void validate()})
onBeforeUnmount(()=>{sequence++;clearTimeout(timer);if(downloadUrl.value)URL.revokeObjectURL(downloadUrl.value)})
function visibility(key:string){const v=props.profile.visibility;return typeof v==='object'&&v!==null&&key in v&&v[key as keyof typeof v]==='private'?'private':'public'}
function setEdit(value:boolean){emit('edit',value)}
</script>
<template>
  <section id="acs-editor" class="acs-editor" :data-inherited="inherited">
    <div class="cardhead">
      <div><h2>ACS 能力描述</h2><p v-if="!inherited" class="sub">通过基础信息和技能生成完整 ACS。</p></div>
      <div class="actions">
        <button v-if="online && !inherited" type="button" class="button small" :disabled="locked" @click="setEdit(!editing)">{{ editing?'取消修改':'修改' }}</button>
        <span class="tag">{{ state }}</span>
      </div>
    </div>
    <p v-if="inherited" class="sub">正式技能、能力描述和提供者由个人分身模板继承。个人资料仍按原流程填写并审核。</p>
    <p v-else class="sub">ACS 用于对外描述分身能力。上线后修改须重新审核，正式身份和通信端点由注册流程管理。</p>
    <div class="acs-layout">
      <div v-if="inherited">
        <h3>继承的技能</h3>
        <section v-for="skill in inheritedSkills" :key="skill.id" class="acs-skill">
          <h3>{{ skill.name }}</h3><MarkdownContent :source="skill.description || ''" :diagrams="false"/><small class="mono">{{ skill.id }}</small>
        </section>
        <p v-if="!inheritedSkills.length" class="sub">等待个人分身模板就绪。</p>
      </div>
      <div v-else-if="modelValue" class="acs-controls">
        <div class="acs-tabs" role="tablist" aria-label="ACS 配置">
          <button v-for="key in (['basic','skills','provider'] as const)" :id="'acs-tab-'+key" :key="key" type="button" role="tab" :aria-controls="'acs-panel-'+key" :aria-selected="tab===key" @click="tab=key">{{ {basic:'基础信息',skills:'技能定义',provider:'提供者'}[key] }}<span v-if="key==='skills'">{{ modelValue.skills.length }}</span></button>
        </div>
        <fieldset class="acs-edit-fields" :disabled="locked || !editing">
          <div v-show="tab==='basic'" id="acs-panel-basic" role="tabpanel" aria-labelledby="acs-tab-basic">
            <div class="formgrid">
              <label v-for="(label,key) in basicFields" :key="key" class="field" :class="{wide:key==='description'}">{{ label }}
                <textarea placeholder=" " class="form-control" v-if="key==='description'" v-model="modelValue.description" rows="4" maxlength="2000"/>
                <input placeholder=" " class="form-control" v-else v-model="modelValue[key]" :maxlength="key==='name'?100:key==='version'?80:500">
                <span v-if="key==='description' && editing" class="acs-field-footer">
                  <small>说明分身可提供的服务及适用范围。</small>
                  <button type="button" class="acs-description-reset" title="恢复生成的能力描述" aria-label="恢复生成的能力描述" @click="validate(true)">↶</button>
                </span>
              </label>
            </div>
            <div class="grid2 acs-formats">
              <MimeEditor v-model="modelValue.default_input_modes" label="默认输入格式"/>
              <MimeEditor v-model="modelValue.default_output_modes" label="默认输出格式"/>
            </div>
            <AcsDeployment :document="preview"/>
          </div>
          <div v-show="tab==='skills'" id="acs-panel-skills" role="tabpanel" aria-labelledby="acs-tab-skills">
            <div class="cardhead"><h3>技能定义</h3><button type="button" class="button small" :disabled="modelValue.skills.length>=20" @click="addSkill">添加技能</button></div>
            <section v-for="(skill,i) in modelValue.skills" :key="skill.id" class="acs-skill">
              <div class="cardhead"><span class="mono acs-skill-id">{{ skill.id }}</span><span v-if="required(skill)" class="tag">系统必备</span><button v-else type="button" class="button small danger" @click="modelValue.skills.splice(i,1)">删除技能</button></div>
              <fieldset class="acs-edit-fields" :disabled="required(skill)">
                <div class="formgrid">
                  <label class="field">技能名称 *<input placeholder=" " class="form-control" v-model="skill.name" maxlength="100"></label>
                  <label class="field">版本 *<input placeholder=" " class="form-control" v-model="skill.version" maxlength="80"></label>
                  <div class="field wide">
                    <template v-if="editing && !locked && !required(skill)">
                      <label :for="'acs-description-'+skill.id">技能描述 *</label>
                      <div class="form-control-group">
                        <span :id="'acs-prefix-'+skill.id" class="skill-description-prefix">{{ skillPrefix }}</span>
                        <textarea :id="'acs-description-'+skill.id" :aria-describedby="'acs-prefix-'+skill.id"
                          :value="suffix(skill.description)" rows="3" maxlength="2000" @input="updateSkill(skill,$event)"/>
                      </div>
                    </template>
                    <template v-else>
                      <span>技能描述 *</span>
                      <p class="form-readout">{{ skill.description }}</p>
                    </template>
                  </div>
                  <AcsListField :key="'tags:'+editing" v-model="skill.tags" :rows="2" comma-separated>标签（每行一项，也可用逗号分隔）</AcsListField>
                  <AcsListField :key="'examples:'+editing" v-model="skill.examples" :rows="3">提问示例（每行一条）</AcsListField>
                </div>
                <div class="grid2 acs-formats"><MimeEditor v-model="skill.input_modes" label="技能输入格式"/><MimeEditor v-model="skill.output_modes" label="技能输出格式"/></div>
              </fieldset>
            </section>
          </div>
          <div v-show="tab==='provider'" id="acs-panel-provider" role="tabpanel" aria-labelledby="acs-tab-provider">
            <div class="formgrid"><label v-for="(label,key) in providerFields" :key="key" class="field">{{ label }}<input placeholder=" " class="form-control" v-model="modelValue.provider[key]" :readonly="direct && (key==='name'||key==='organization')"></label></div>
            <div class="cardhead acs-formats"><h3>域名备案</h3><button type="button" class="button small" :disabled="modelValue.provider.domain_registrations.length>=20" @click="modelValue.provider.domain_registrations.push({domain:'',registration_number:'',registration_type:'ICP'})">添加域名备案</button></div>
            <section v-for="(domain,i) in modelValue.provider.domain_registrations" :key="i" class="acs-domain">
              <div class="formgrid">
                <label class="field">主域名<input placeholder=" " class="form-control" v-model="domain.domain" maxlength="253"></label>
                <label class="field">备案号 / 注册号<input placeholder=" " class="form-control" v-model="domain.registration_number" maxlength="160"></label>
                <label class="field">注册类型<select class="form-control" v-model="domain.registration_type"><option>ICP</option><option>WHOIS</option></select></label>
                <div class="actions"><button type="button" class="button small danger" @click="modelValue.provider.domain_registrations.splice(i,1)">删除条目</button></div>
              </div>
            </section>
          </div>
        </fieldset>
      </div>
      <aside class="acs-preview">
        <div class="cardhead"><h3>{{ inherited ? (registered?'正式 ACS':'模板 ACS') : '完整 ACS JSON' }}</h3><button v-if="editing && !inherited && modelValue" type="button" class="button small" :disabled="locked" @click="validate()">校验并预览</button></div>
        <p v-if="errors" class="sub" role="status">{{ errors }}</p>
        <pre v-if="preview" class="acs-json" tabindex="0" aria-label="完整 ACS JSON 预览">{{ JSON.stringify(preview,null,2) }}</pre>
        <a v-if="valid && downloadUrl" class="button small" :href="downloadUrl" download="avatar-acs.json">下载 ACS</a>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.skill-description-prefix {
  flex: 0 1 auto;
  max-width: 40%;
  padding: .125rem .375rem;
  border-radius: 4px;
  background: var(--accent-subtle);
  color: var(--accent);
  font-size: var(--type-control);
  line-height: 1.5;
  overflow-wrap: anywhere;
}
</style>
