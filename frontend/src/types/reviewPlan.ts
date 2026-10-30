import type { ReviewLimits, ReviewOptions } from './review';

export type { ReviewLimits };
export type ReviewPlanOptions = ReviewOptions;

export interface ReviewCapabilities {
  depth_presets: {
    id: ReviewPlanOptions['depth'];
    label: string;
    limits: ReviewLimits;
  }[];
  focus_categories: ReviewPlanOptions['focus'];
  limits: {
    defaults: ReviewLimits;
    maxima: ReviewLimits;
  };
  pr_discussions_available?: boolean;
  pr_discussions_default?: boolean;
}
