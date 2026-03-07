import { 
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, 
  BarChart, Bar, Cell 
} from 'recharts';
import { 
  Activity, Database, Layers, RefreshCw, Clock, 
  BarChart3, LayoutDashboard 
} from 'lucide-react';
import { useFilterStore } from './store/useFilterStore';
import { useDailyCounts, useSummary, useEventTypes } from './hooks/useAnalytics';

const App: React.FC = () => {
  const { 
    startDate, endDate, source, eventType, 
    setStartDate, setEndDate, setSource, setEventType, resetFilters 
  } = useFilterStore();

  const { data: dailyData, isLoading: dailyLoading } = useDailyCounts();
  const { data: summaryData, isLoading: summaryLoading } = useSummary();
  const { data: typeList } = useEventTypes();

  // Mocked sources since there's no endpoint to list distinct sources yet
  const sources = ['web', 'mobile', 'api', 'worker'];

  if (dailyLoading || summaryLoading) {
    return (
      <div className="loading-spinner">
        <RefreshCw className="animate-spin" />
        <span style={{ marginLeft: '1rem' }}>Loading EventFlux Analytics...</span>
      </div>
    );
  }

  const chartData = dailyData?.data?.map((d: any) => ({
    day: d.day,
    count: d.count,
  })).reverse() || [];

  const COLORS = ['#10b981', '#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899'];

  return (
    <div className="dashboard-container">
      {/* --- HEADER --- */}
      <header className="glass-card controls-bar">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <LayoutDashboard color="#10b981" size={32} />
          <div>
            <h1>EventFlux</h1>
            <p className="stat-label" style={{ fontSize: '0.75rem' }}>Real-time Observability</p>
          </div>
        </div>

        <div className="filters-group">
          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
            <Clock size={16} color="#94a3b8" />
            <input 
              type="date" 
              value={startDate} 
              onChange={(e) => setStartDate(e.target.value)} 
            />
            <span style={{ color: '#94a3b8' }}>to</span>
            <input 
              type="date" 
              value={endDate} 
              onChange={(e) => setEndDate(e.target.value)} 
            />
          </div>

          <select value={source || ''} onChange={(e) => setSource(e.target.value || null)}>
            <option value="">All Sources</option>
            {sources.map(s => <option key={s} value={s}>{s}</option>)}
          </select>

          <select value={eventType || ''} onChange={(e) => setEventType(e.target.value || null)}>
            <option value="">All Event Types</option>
            {typeList?.data?.map((t: string) => <option key={t} value={t}>{t}</option>)}
          </select>

          <button 
            onClick={resetFilters}
            style={{ 
              background: 'transparent', border: 'none', color: '#ef4444', 
              cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.25rem' 
            }}
          >
            <RefreshCw size={14} /> Reset
          </button>
        </div>
      </header>

      {/* --- SUMMARY STATS --- */}
      <div className="stats-grid">
        <div className="glass-card stat-card">
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span className="stat-label">Total Events</span>
            <Activity color="#10b981" size={20} />
          </div>
          <span className="stat-value">{summaryData?.total_events?.toLocaleString()}</span>
        </div>

        <div className="glass-card stat-card">
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span className="stat-label">Event Types</span>
            <Layers color="#06b6d4" size={20} />
          </div>
          <span className="stat-value">{summaryData?.unique_event_types}</span>
        </div>

        <div className="glass-card stat-card">
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span className="stat-label">Active Sources</span>
            <Database color="#3b82f6" size={20} />
          </div>
          <span className="stat-value">{summaryData?.unique_sources}</span>
        </div>
      </div>

      {/* --- TREND CHART --- */}
      <div className="glass-card">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1.5rem' }}>
          <BarChart3 color="#10b981" size={20} />
          <h3>Daily Volume Trend</h3>
        </div>
        <div className="main-chart">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" vertical={false} />
              <XAxis 
                dataKey="day" 
                stroke="#64748b" 
                fontSize={12} 
                tickMargin={10}
              />
              <YAxis 
                stroke="#64748b" 
                fontSize={12} 
                tickFormatter={(val) => val >= 1000 ? `${(val/1000).toFixed(1)}k` : val}
              />
              <Tooltip 
                contentStyle={{ 
                  backgroundColor: '#1e293b', border: '1px solid rgba(255,255,255,0.1)',
                  borderRadius: '0.5rem', boxShadow: '0 10px 15px -3px rgba(0,0,0,0.5)'
                }}
                itemStyle={{ color: '#10b981' }}
              />
              <Line 
                type="monotone" 
                dataKey="count" 
                stroke="#10b981" 
                strokeWidth={3} 
                dot={{ r: 4, fill: '#10b981', strokeWidth: 0 }}
                activeDot={{ r: 6, stroke: 'rgba(16,185,129,0.3)', strokeWidth: 8 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* --- BREAKDOWNS --- */}
      <div className="top-aggregates">
        <div className="glass-card">
          <h3 style={{ marginBottom: '1rem' }}>Top Event Types</h3>
          <div style={{ height: '300px' }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={summaryData?.top_event_types} layout="vertical">
                <XAxis type="number" hide />
                <YAxis dataKey="event_type" type="category" stroke="#94a3b8" fontSize={12} width={100} />
                <Tooltip 
                   contentStyle={{ backgroundColor: '#1e293b', border: '1px solid rgba(255,255,255,0.1)' }}
                />
                <Bar dataKey="total" radius={[0, 4, 4, 0]}>
                  {summaryData?.top_event_types?.map((_: any, index: number) => (
                    <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="glass-card">
          <h3 style={{ marginBottom: '1rem' }}>Source Distribution</h3>
          <div style={{ height: '300px' }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={summaryData?.top_sources}>
                <XAxis dataKey="source" stroke="#94a3b8" fontSize={12} />
                <YAxis hide />
                <Tooltip 
                   contentStyle={{ backgroundColor: '#1e293b', border: '1px solid rgba(255,255,255,0.1)' }}
                />
                <Bar dataKey="total" radius={[4, 4, 0, 0]}>
                  {summaryData?.top_sources?.map((_: any, index: number) => (
                    <Cell key={`cell-${index}`} fill={COLORS[(index + 2) % COLORS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </div>
  );
};

export default App;
