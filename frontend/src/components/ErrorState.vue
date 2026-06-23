<template>
  <el-alert v-if="message" type="error" :closable="false" show-icon>
    <template #title>{{ message }}</template>
    <div v-if="requestId">request_id: {{ requestId }}</div>
  </el-alert>
</template>

<script setup lang="ts">
defineProps<{ message: string; requestId?: string }>()
</script>
