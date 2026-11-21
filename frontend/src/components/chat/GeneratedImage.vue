<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { usePreviewZoom } from '../../composables/usePreviewZoom'
import { request } from '../../api/client'
import { ElMessage } from 'element-plus'
import MaterialFileDialog from '../MaterialFileDialog.vue'
import type { ChatFile, FilePromotion } from '../../types'
import AppIcon from '../AppIcon.vue'
import DiagramMenu from '../DiagramMenu.vue'
import PreviewZoom from '../PreviewZoom.vue'

const props = defineProps<{ file: ChatFile; url: string }>()
defineEmits<{ preview: [file: ChatFile] }>()
const viewport = ref<HTMLElement>(), sheet = ref<HTMLElement>()
const { naturalWidth, scale, percent, fit, setScale, resetZoom } = usePreviewZoom(viewport, sheet, ref('image'))
const failed = ref(false), promoting = ref(false), saved = ref(false)
const previewURL = computed(() => props.file.origin === 'material' ? props.url : props.url + '?preview=true')

watch(() => props.url, () => { failed.value = false; saved.value = false; promoting.value = false; resetZoom() })
async function save(value: FilePromotion) {
  if (props.file.origin === 'material') return
  const url = props.url
  await request(url.slice(4)+'/promote','POST',value)
  if (url !== props.url) return
  saved.value = true
  promoting.value = false
  ElMessage.success('已保存到资料库')
}
</script>

<template>
  <figure class="mermaid-card generated-image">
    <header>
      <small :title="file.filename">{{ file.filename }}</small>
      <DiagramMenu :allow-save="file.origin !== 'material'" :save-disabled="promoting" :saved="saved || !!file.promoted_id" @save="promoting=true">
        <a :href="url" :download="file.filename"><AppIcon name="download" :size="15"/>下载图片</a>
      </DiagramMenu>
    </header>
    <div v-if="!failed" class="image-tools">
      <PreviewZoom :percent="percent" :fit="fit" @scale="setScale"/>
    </div>
    <div v-show="!failed" ref="viewport" class="image-viewport">
      <button type="button" ref="sheet" class="image-sheet" :style="{ width: naturalWidth ? naturalWidth * scale + 'px' : '100%' }"
        :aria-label="'预览 '+file.filename" @click="$emit('preview', file)">
        <img :src="previewURL" :alt="file.filename" loading="lazy" decoding="async"
          @load="naturalWidth = ($event.target as HTMLImageElement).naturalWidth" @error="failed = true">
      </button>
    </div>
    <button v-if="failed" type="button" class="button small image-tools" @click="$emit('preview', file)">预览原图</button>
  </figure>
  <MaterialFileDialog v-if="promoting" :filename="file.filename" :save="save" @close="promoting=false"/>
</template>

<style scoped>
.generated-image { max-inline-size:100%; }
.generated-image header small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.image-tools { display:flex; gap:.5rem; margin:.5rem; }
.image-viewport { max-block-size:55dvh; overflow:auto; overflow-anchor:none; }
.image-sheet { display:block; margin-inline:auto; padding:0; border:0; background:none; cursor:zoom-in; }
.image-sheet img { display:block; inline-size:100%; max-inline-size:none; block-size:auto; }
</style>
