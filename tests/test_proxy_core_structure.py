def test_proxy_core_is_facade_over_proxy_package():
    import proxy.channel_registry
    import proxy.conversion
    import proxy.errors
    import proxy.non_stream_executor
    import proxy.routing
    import proxy.stream_executor
    import proxy.stream_reconstruct
    import proxy.stream_sse
    import proxy_core

    assert proxy_core.proxy_request is proxy.routing.proxy_request
    assert proxy_core.AllChannelsExhausted is proxy.errors.AllChannelsExhausted
    assert proxy_core.ConverterError is proxy.errors.ConverterError
    assert proxy_core._registry_get_channels_for_model is proxy.channel_registry.get_channels_for_model
    assert proxy_core._conversion.get_converter_and_upstream_type is (
        proxy.conversion.get_converter_and_upstream_type
    )
    assert proxy_core._do_request is proxy.non_stream_executor.do_request
    assert proxy_core._do_stream_request is proxy.stream_executor.do_stream_request
    assert proxy_core._build_openai_stream_response is (
        proxy.stream_reconstruct.build_openai_stream_response
    )
    assert proxy_core._iter_sse_blocks is proxy.stream_sse.iter_sse_blocks
