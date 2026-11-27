<script setup lang="ts">
import { computed } from 'vue'
const props=defineProps<{modelValue:string[];label:string;disabled?:boolean}>()
const emit=defineEmits<{ 'update:modelValue':[value:string[]] }>()
const choices=[['text/plain','纯文本'],['application/json','JSON 数据'],['text/markdown','Markdown 文本']]
const custom=computed(()=>props.modelValue.filter(value=>!choices.some(([id])=>id===value)).join('\n'))
function toggle(id:string,checked:boolean){emit('update:modelValue',checked?[...new Set([...props.modelValue,id])]:props.modelValue.filter(value=>value!==id))}
function edit(event:Event){const target=event.target;if(!(target instanceof HTMLTextAreaElement))return;emit('update:modelValue',[...new Set([...props.modelValue.filter(value=>choices.some(([id])=>id===value)),...target.value.split('\n').map(value=>value.trim()).filter(Boolean)])])}
</script>
<template>
  <fieldset class="acs-mime" :disabled="disabled">
    <legend>{{ label }}</legend>
    <div class="acs-mime-options">
      <label v-for="[id, title] in choices" :key="id">
        <input
          type="checkbox"
          :checked="modelValue.includes(id)"
          @change="toggle(id, ($event.target as HTMLInputElement).checked)"
        />{{ title }} <small>{{ id }}</small>
      </label>
    </div>
    <label class="field">其他格式（每行一项）<textarea placeholder=" " class="form-control" :value="custom" rows="2" @change="edit" />
    </label>
  </fieldset>
</template>
