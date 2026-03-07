import { create } from 'zustand';
import { subDays, format } from 'date-fns';

interface FilterState {
  startDate: string;
  endDate: string;
  source: string | null;
  eventType: string | null;
  setStartDate: (date: string) => void;
  setEndDate: (date: string) => void;
  setSource: (source: string | null) => void;
  setEventType: (eventType: string | null) => void;
  resetFilters: () => void;
}

const getDefaultStartDate = () => format(subDays(new Date(), 7), "yyyy-MM-dd");
const getDefaultEndDate = () => format(new Date(), "yyyy-MM-dd");

export const useFilterStore = create<FilterState>((set) => ({
  startDate: getDefaultStartDate(),
  endDate: getDefaultEndDate(),
  source: null,
  eventType: null,
  setStartDate: (startDate) => set({ startDate }),
  setEndDate: (endDate) => set({ endDate }),
  setSource: (source) => set({ source }),
  setEventType: (eventType) => set({ eventType }),
  resetFilters: () => set({
    startDate: getDefaultStartDate(),
    endDate: getDefaultEndDate(),
    source: null,
    eventType: null,
  }),
}));
