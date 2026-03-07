import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { useFilterStore } from '../store/useFilterStore';

const api = axios.create({
  baseURL: '/v1',
});

export const useDailyCounts = () => {
  const { startDate, endDate, source, eventType } = useFilterStore();
  
  return useQuery({
    queryKey: ['analytics', 'daily', { startDate, endDate, source, eventType }],
    queryFn: async () => {
      const response = await api.get('/analytics/events/daily', {
        params: {
          start_date: startDate,
          end_date: endDate,
          source: source || undefined,
          event_type: eventType || undefined,
          limit: 1000,
        },
      });
      return response.data;
    },
  });
};

export const useSummary = () => {
  const { startDate, endDate } = useFilterStore();
  
  return useQuery({
    queryKey: ['analytics', 'summary', { startDate, endDate }],
    queryFn: async () => {
      const response = await api.get('/analytics/events/summary', {
        params: {
          start_date: startDate,
          end_date: endDate,
        },
      });
      return response.data;
    },
  });
};

export const useEventTypes = () => {
  return useQuery({
    queryKey: ['analytics', 'event-types'],
    queryFn: async () => {
      const response = await api.get('/analytics/event-types');
      return response.data;
    },
  });
};
