<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ rows: number; commaSeparated?: boolean }>()
const model = defineModel<string[]>({ required: true })
const text = ref(model.value.join('\n'))
const parse = (value: string) => value.split(props.commaSeparated ? /[\n,，]/ : /\n/).map(item => item.trim()).filter(Boolean)

watch(text, value => {
  const items = parse(value)
  if (items.join('\n') !== model.value.join('\n')) model.value = items
}, { flush: 'sync' })
watch(model, value => {
  if (value.join('\n') !== parse(text.value).join('\n')) text.value = value.join('\n')
})
</script>

<template>
  <label class="field wide"><slot/><textarea v-model="text" placeholder=" " class="form-control" :rows="rows"/></label>
</template>
