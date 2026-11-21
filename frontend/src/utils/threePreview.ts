import webgl from '../../node_modules/three/build/three.module.js?raw'
import core from '../../node_modules/three/build/three.core.js?raw'

const dataModule = (source: string) => 'data:text/javascript;charset=utf-8,' + encodeURIComponent(source)

// Relative imports do not resolve from a data URL.
export const threeImports = JSON.stringify({
  imports: {
    three: dataModule(webgl.replaceAll('./three.core.js', 'three-core')),
    'three-core': dataModule(core),
  },
})
