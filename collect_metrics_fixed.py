def collect_metrics():
    """Collect all hardware metrics"""
    with collection_lock:
        try:
            print("  Collecting metrics...")
            
            now = datetime.now()
            metric = {
                'timestamp': datetime.now().isoformat()
            }
            
            # CPU Temperature
            cpu_temps = {}
            try:
                sensors = psutil.sensors_temperatures()
                for sensor, readings in sensors.items():
                    for reading in readings:
                        cpu_temps[sensor] = reading.current
            except:
                pass
            metric['cpu_temp'] = cpu_temps.get('k10temp', 0)
            
            # CPU Frequency
            cpu_freq = psutil.cpu_freq()
            metric['cpu_freq'] = cpu_freq.current if cpu_freq else 0
            
            # CPU Usage
            metric['cpu_percent'] = psutil.cpu_percent(interval=None)
            
            # RAM
            ram = psutil.virtual_memory()
            metric['ram_percent'] = ram.percent
            
            # Disk
            disk = psutil.disk_usage('/')
            metric['disk_percent'] = disk.percent
            
            # Network
            net_io = psutil.net_io_counters()
            metric['network_rx'] = net_io.bytes_recv
            metric['network_tx'] = net_io.bytes_sent
            
            # GPU
            gpu_data = []
            if device_count > 0:
                for i in range(device_count):
                    try:
                        handle = nvmlDeviceGetHandleByIndex(i)
                        temp = nvmlDeviceGetTemperature(handle, 0)
                        mem_info = nvmlDeviceGetMemoryInfo(handle)
                        util = nvmlDeviceGetUtilizationRates(handle)
                        name = nvmlDeviceGetName(handle)
                        
                        gpu_data.append({
                            'index': i,
                            'name': name.decode() if isinstance(name, bytes) else name,
                            'temperature': temp,
                            'memory_used': mem_info.used,
                            'memory_total': mem_info.total,
                            'utilization_gpu': util.gpu,
                            'utilization_memory': util.memory
                        })
                    except:
                        pass
            metric['gpu'] = gpu_data
            
            # Add to in-memory history
            metrics_history['cpu_temps'].append(cpu_temps)
            metrics_history['cpu_freqs'].append(cpu_freq)
            metrics_history['cpu_percent'].append(metric['cpu_percent'])
            metrics_history['ram'].append(metric['ram_percent'])
            metrics_history['disk'].append(metric['disk_percent'])
            metrics_history['network'].append({'rx': metric['network_rx'], 'tx': metric['network_tx']})
            metrics_history['gpu'].append(gpu_data)
            
            # Add to database
            db.insert_metric(metric)
            
            print("  ✓ Metrics collected successfully")
            
            # Check and report alerts
            check_alerts(metric)
            
        except Exception as e:
            print(f"  ✗ Error collecting metrics: {e}")
            import traceback
            traceback.print_exc()
