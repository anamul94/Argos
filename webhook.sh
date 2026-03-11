aws events update-api-destination \
  --name Argos-webhook-dest \
  --invocation-endpoint "https://your-new-webhook-url.com/alert-webhook" \
  --http-method POST \
  --region ap-south-1
