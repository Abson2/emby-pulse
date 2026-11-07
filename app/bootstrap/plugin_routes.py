def register_calendar_notify_routes(app) -> None:
    from app.domains.notifications import calendar_notify

    app.include_router(calendar_notify.router)


def register_plugin_routes(app) -> None:
    from app.domains.plugins import router as plugins_router
    from app.plugins import discover_plugins, get_enabled_plugins

    discover_plugins()
    for plugin in get_enabled_plugins():
        try:
            app.include_router(plugin.router)
        except Exception as e:
            print(f"[🧩 插件] 注册路由失败: {plugin.id} - {e}")

    app.include_router(plugins_router.router)
    plugins_router.set_app(app)

    # 2.5：插件中心「定时执行」按分钟间隔调度器（幂等；只对已加载插件触发 on_schedule）
    try:
        from app.plugins.scheduler import start_plugin_scheduler
        start_plugin_scheduler()
    except Exception as e:
        print(f"[🧩 插件] 定时调度器启动失败: {e}")

    # 2.5.1：cron 定时计划调度器（北京时间 UTC+8 / 防并发 / 离线不补跑）
    try:
        from app.plugins.schedule_plans import start_plugin_plan_scheduler
        start_plugin_plan_scheduler()
    except Exception as e:
        print(f"[🧩 插件] 定时计划调度器启动失败: {e}")
