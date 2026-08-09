<script setup lang="ts">
import { computed } from "vue";
import { Listbox, ListboxButton, ListboxOption, ListboxOptions } from "@headlessui/vue";
import AppIcon from "./AppIcon.vue";

type SelectOption = {
  value: string;
  label: string;
  description?: string;
};

const props = withDefaults(defineProps<{
  modelValue: string;
  options: SelectOption[];
  disabled?: boolean;
  compact?: boolean;
  ariaLabel?: string;
  ariaDescribedby?: string;
}>(), {
  disabled: false,
  compact: false,
  ariaLabel: undefined,
  ariaDescribedby: undefined,
});

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();

const valueModel = computed({
  get: () => props.modelValue,
  set: (value: string) => emit("update:modelValue", value),
});

const selectedOption = computed(
  () => props.options.find((option) => option.value === props.modelValue) ?? props.options[0],
);
</script>

<template>
  <Listbox v-slot="{ open }" v-model="valueModel" as="div" class="app-select" :disabled="disabled">
    <ListboxButton
      class="app-select__trigger"
      :class="{ 'app-select__trigger--compact': compact, 'is-open': open }"
      :aria-label="ariaLabel"
      :aria-describedby="ariaDescribedby"
    >
      <span class="app-select__value">
        <strong>{{ selectedOption?.label }}</strong>
        <small v-if="selectedOption?.description && !compact">{{ selectedOption.description }}</small>
      </span>
      <span class="app-select__chevron" aria-hidden="true">
        <AppIcon name="chevron-down" :size="17" :stroke-width="2.2" />
      </span>
    </ListboxButton>

    <Transition name="select-pop">
      <ListboxOptions class="app-select__options">
        <ListboxOption
          v-for="option in options"
          :key="option.value"
          v-slot="{ active, selected }"
          as="template"
          :value="option.value"
        >
          <li
            class="app-select__option"
            :class="{ 'is-active': active, 'is-selected': selected }"
          >
            <span class="app-select__option-copy">
              <strong>{{ option.label }}</strong>
              <small v-if="option.description && !compact">{{ option.description }}</small>
            </span>
            <span v-if="selected" class="app-select__check" aria-hidden="true">
              <AppIcon name="check" :size="16" :stroke-width="2.4" />
            </span>
          </li>
        </ListboxOption>
      </ListboxOptions>
    </Transition>
  </Listbox>
</template>
