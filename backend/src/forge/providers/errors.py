class ProviderError(Exception):
    code = "provider_error"
    public_message = "The model provider request failed."


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    public_message = "The configured model provider is unavailable."


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    public_message = "The model provider request timed out."


class ProviderRequestError(ProviderError):
    code = "provider_request_failed"
    public_message = "The model provider rejected the request."


class ProviderResponseError(ProviderError):
    code = "provider_invalid_response"
    public_message = "The model provider returned an invalid response."


class ProviderResponseTooLargeError(ProviderError):
    code = "provider_response_too_large"
    public_message = "The model provider response exceeded the configured limit."
